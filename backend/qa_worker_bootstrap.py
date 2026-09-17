from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "qa-alert.db"


def database_ready() -> bool:
    try:
        if not DB.exists():
            return False
        with sqlite3.connect(DB, timeout=5) as c:
            row = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cases'").fetchone()
            return bool(row)
    except Exception:
        return False


while not database_ready():
    print("[QA WORKER] Waiting for QA ALERT database...")
    time.sleep(2)

import backend.qa_worker as qa_worker
from backend.booking_rules import read_automation_settings
from backend.knowledge_loader import load_knowledge_text

# Keep private Excel/Word Matrix files local and load them automatically.
qa_worker._knowledge_text = load_knowledge_text

# Respect the self-service setting when multiple bookings cannot be matched to one.
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
        finding = "Multiple bookings were found, but one unique booking could not be matched to the call. QA was not run on multiple bookings."
        c.execute(
            "UPDATE cases SET booking_match_status=?,booking_match_confidence=?,status=?,finding=?,updated_at=? WHERE id=?",
            (status, confidence, new_status, finding, now, case_row["id"]),
        )


qa_worker._save_match = _save_match_with_setting

# URGENT QA RULE:
# Any case that already has a transcript but no QA result must be picked up,
# regardless of an old/legacy status. Only real holds/errors/current work are excluded.
def _urgent_next_case():
    qa_worker.ensure_booking_schema()
    with qa_worker.db() as c:
        row = c.execute(
            """
            SELECT * FROM cases
            WHERE COALESCE(transcript_text,'')<>''
              AND COALESCE(qa_result_json,'')=''
              AND status NOT IN (
                'qa_running','qa_error','booking_match_hold','booking_match_attention'
              )
            ORDER BY COALESCE(transcription_completed_at,updated_at,created_at) ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        now = qa_worker.utc_now()
        c.execute(
            "UPDATE cases SET status='qa_running',updated_at=? WHERE id=?",
            (now, row["id"]),
        )
        return c.execute("SELECT * FROM cases WHERE id=?", (row["id"],)).fetchone()


qa_worker.next_case = _urgent_next_case

# Send a positive Slack notification too, so a clean QA is visible and testable.
_original_process_case = qa_worker.process_case


def _send_good_job_slack(case_row) -> bool:
    with qa_worker.db() as c:
        raw = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    if raw.get("slack_enabled", "false") != "true":
        return False
    recipient = raw.get("slack_recipient_id", "").strip()
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    if not recipient or not token:
        return False

    itinerary = case_row["itinerary"] or "N/A"
    agent = case_row["agent"] or "N/A"
    center = case_row["call_center"] or "N/A"
    score = case_row["score"]
    no_booking = int(case_row["booking_found"] or 0) == 0

    if no_booking:
        result_line = "Behavior review completed with no customer-service conduct issue found."
    else:
        result_line = "QA completed with no issue found."
    if score is not None:
        result_line += f" Score: {score}."

    text = (
        "Good Job\n"
        f"Booking: {itinerary}\n"
        f"Agent: {agent} from {center}\n"
        f"Result: {result_line}"
    )

    from slack_sdk import WebClient

    client = WebClient(token=token)
    channel = recipient
    if recipient.startswith(("U", "W")):
        channel = client.conversations_open(users=[recipient])["channel"]["id"]
    client.chat_postMessage(channel=channel, text=text)
    return True


def _process_case_with_good_job(row):
    _original_process_case(row)
    with qa_worker.db() as c:
        fresh = c.execute("SELECT * FROM cases WHERE id=?", (row["id"],)).fetchone()
    if not fresh:
        return

    # Problem alerts are already sent by qa_worker.process_case().
    # For a clean result, send one Good Job message and mark it as sent.
    if fresh["qa_result_json"] and fresh["status"] in {"completed", "completed_no_booking"} and not fresh["slack_alert_sent_at"]:
        try:
            if _send_good_job_slack(fresh):
                qa_worker._mark_slack_sent(fresh["id"])
        except Exception as exc:
            print(f"[QA WORKER] Good Job Slack message failed for {fresh['call_id']}: {exc}")


qa_worker.process_case = _process_case_with_good_job
qa_worker.main()
