from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "qa-alert.db"
REPORT_URL = "https://qa-alert.netlify.app/"
REPORT_LOGIN = "qateam2026"
REPORT_FOOTER = (
    "\n\nQA Report: " + REPORT_URL +
    "\nLogin: " + REPORT_LOGIN +
    "\nPassword: use the QA ALERT password shared separately."
)


def database_ready() -> bool:
    try:
        if not DB.exists():
            return False
        with sqlite3.connect(DB, timeout=5) as c:
            return bool(c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cases'").fetchone())
    except Exception:
        return False


while not database_ready():
    print("[QA WORKER] Waiting for QA ALERT database...")
    time.sleep(2)

import backend.qa_worker as qa_worker
import backend.qa_pipeline as qa_pipeline
from backend.booking_rules import alert_text, read_automation_settings
from backend.knowledge_loader import load_knowledge_text

qa_worker._knowledge_text = load_knowledge_text

# Respect the self-service booking-match setting.
_original_save_match = qa_worker._save_match


def _save_match_with_setting(case_row, chosen, confidence, status):
    if chosen is not None:
        return _original_save_match(case_row, chosen, confidence, status)
    settings = read_automation_settings()
    action = settings.get("multi_booking_no_match_action", "needs_attention")
    now = qa_worker.utc_now()
    with qa_worker.db() as c:
        c.execute("UPDATE booking_candidates SET is_matched=0,match_confidence=NULL WHERE case_id=?", (case_row["id"],))
        new_status = "needs_attention" if action == "needs_attention" else "booking_match_hold"
        finding = "Multiple bookings were found, but one unique booking could not be matched to the call. Booking-specific QA was not run on multiple bookings."
        c.execute(
            "UPDATE cases SET booking_match_status=?,booking_match_confidence=?,status=?,finding=?,updated_at=? WHERE id=?",
            (status, confidence, new_status, finding, now, case_row["id"]),
        )


qa_worker._save_match = _save_match_with_setting

# Any saved transcript without a QA result is urgent QA work, regardless of legacy status.
def _urgent_next_case():
    qa_worker.ensure_booking_schema()
    with qa_worker.db() as c:
        row = c.execute(
            """
            SELECT * FROM cases
            WHERE COALESCE(transcript_text,'')<>''
              AND COALESCE(qa_result_json,'')=''
              AND status NOT IN ('qa_running','qa_error','booking_match_hold')
            ORDER BY COALESCE(transcription_completed_at,updated_at,created_at) ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        now = qa_worker.utc_now()
        c.execute("UPDATE cases SET status='qa_running',updated_at=? WHERE id=?", (now, row["id"]))
        return c.execute("SELECT * FROM cases WHERE id=?", (row["id"],)).fetchone()


qa_worker.next_case = _urgent_next_case


def _slack_destination():
    with qa_worker.db() as c:
        raw = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    if raw.get("slack_enabled", "false") != "true":
        return None, None
    recipient = raw.get("slack_recipient_id", "").strip()
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    if not recipient or not token:
        return None, None
    return recipient, token


def _post_slack(text: str) -> bool:
    recipient, token = _slack_destination()
    if not recipient or not token:
        return False
    from slack_sdk import WebClient
    client = WebClient(token=token)
    channel = recipient
    if recipient.startswith(("U", "W")):
        channel = client.conversations_open(users=[recipient])["channel"]["id"]
    client.chat_postMessage(channel=channel, text=text + REPORT_FOOTER)
    return True


def _send_issue_slack_with_report(case_row, qa: dict) -> bool:
    settings = read_automation_settings()
    if not settings.get("slack_on_qa_issue", True):
        return False
    source = str(qa.get("matrix_source") or "").strip()
    process = str(qa.get("process") or "Unspecified process").strip()
    if source == qa_worker.BEHAVIOR_SOURCE:
        text = (
            "Attention\n"
            f"Booking: {case_row['itinerary'] or 'N/A'}\n"
            f"Agent: {case_row['agent'] or 'N/A'} from {case_row['call_center'] or 'N/A'} did not follow this process: \"{process}\".\n"
            f"Source: {qa_worker.BEHAVIOR_SOURCE}."
        )
    else:
        text = alert_text(case_row["itinerary"], case_row["agent"], case_row["call_center"], process)
    return _post_slack(text)


qa_worker._send_issue_slack = _send_issue_slack_with_report
qa_pipeline.qw._send_issue_slack = _send_issue_slack_with_report


def _send_good_job_slack(case_row) -> bool:
    itinerary = case_row["itinerary"] or "N/A"
    agent = case_row["agent"] or "N/A"
    center = case_row["call_center"] or "N/A"
    score = case_row["score"]
    no_booking = int(case_row["booking_found"] or 0) == 0
    result_line = (
        "Behavior review completed with no customer-service conduct issue found."
        if no_booking else "QA completed with no issue found."
    )
    if score is not None:
        result_line += f" Score: {score}."
    return _post_slack(
        "Good Job\n"
        f"Booking: {itinerary}\n"
        f"Agent: {agent} from {center}\n"
        f"Result: {result_line}"
    )


def _process_case_with_good_job_and_cleanup(row):
    qa_pipeline.process_every_transcript(row)
    with qa_worker.db() as c:
        fresh = c.execute("SELECT * FROM cases WHERE id=?", (row["id"],)).fetchone()
    if not fresh:
        return
    if fresh["qa_result_json"] and fresh["status"] in {"completed", "completed_no_booking"} and not fresh["slack_alert_sent_at"]:
        try:
            if _send_good_job_slack(fresh):
                qa_worker._mark_slack_sent(fresh["id"])
        except Exception as exc:
            print(f"[QA WORKER] Good Job Slack message failed for {fresh['call_id']}: {exc}")


qa_worker.process_case = _process_case_with_good_job_and_cleanup
qa_worker.main()
