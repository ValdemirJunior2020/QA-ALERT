from __future__ import annotations

import os
from pathlib import Path

import backend.qa_worker as qw

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".webm", ".aac", ".flac"}
INBOX = Path.home() / "Downloads" / "QA-CALLS"
REPORT_URL = "https://qa-alert.netlify.app/"
REPORT_LOGIN = "qateam2026"
REPORT_FOOTER = (
    "\n\nQA Report: " + REPORT_URL +
    "\nLogin: " + REPORT_LOGIN +
    "\nPassword: use the QA ALERT password shared separately."
)


def _delete_audio_after_qa(row) -> tuple[bool, list[str], list[str]]:
    targets: set[Path] = set()
    raw = row["audio_path"] if "audio_path" in row.keys() else None
    if raw:
        targets.add(Path(raw))
    try:
        for p in INBOX.rglob(f"*{row['call_id']}*"):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
                targets.add(p)
    except Exception:
        pass

    deleted, errors = [], []
    for p in targets:
        try:
            if p.exists():
                p.unlink()
                deleted.append(str(p))
        except Exception as exc:
            errors.append(f"{p}: {exc}")

    now = qw.utc_now()
    with qw.db() as c:
        if not errors:
            c.execute(
                "UPDATE cases SET audio_deleted_at=?,audio_path=NULL,updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
        else:
            c.execute(
                "UPDATE cases SET transcription_error=?,updated_at=? WHERE id=?",
                ("Audio cleanup after QA: " + " | ".join(errors), now, row["id"]),
            )
    return not errors, deleted, errors


def _slack_destination():
    with qw.db() as c:
        raw = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    if raw.get("slack_enabled", "false") != "true":
        return None, None
    recipient = raw.get("slack_recipient_id", "").strip()
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    if not recipient or not token:
        return None, None
    return recipient, token


def _mark_system_slack_error(case_id: int, exc: Exception):
    message = str(exc)
    # Keep this visible in Manager Attention without turning it into an agent QA failure.
    now = qw.utc_now()
    with qw.db() as c:
        row = c.execute("SELECT finding,severity FROM cases WHERE id=?", (case_id,)).fetchone()
        existing = (row["finding"] or "").strip() if row else ""
        system_line = f"System / Slack Error: {message}"
        if system_line not in existing:
            finding = (existing + ("\n" if existing else "") + system_line).strip()
        else:
            finding = existing
        severity = row["severity"] if row and row["severity"] else "info"
        # Preserve real QA severity. A Slack problem by itself remains info.
        c.execute(
            "UPDATE cases SET status='needs_attention',severity=?,finding=?,updated_at=? WHERE id=?",
            (severity, finding, now, case_id),
        )
    print(f"[QA WORKER] Slack delivery failed for case {case_id}: {message}")


def _post_slack(case_id: int, text: str) -> bool:
    recipient, token = _slack_destination()
    if not recipient or not token:
        return False
    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        channel = recipient
        if recipient.startswith(("U", "W")):
            channel = client.conversations_open(users=[recipient])["channel"]["id"]
        client.chat_postMessage(channel=channel, text=text + REPORT_FOOTER)
        qw._mark_slack_sent(case_id)
        return True
    except Exception as exc:
        _mark_system_slack_error(case_id, exc)
        return False


def _issue_text(case_row, qa: dict) -> str:
    source = str(qa.get("matrix_source") or "").strip()
    process = str(qa.get("process") or "Unspecified process").strip()
    if source == qw.BEHAVIOR_SOURCE:
        return (
            "Attention\n"
            f"Booking: {case_row['itinerary'] or 'N/A'}\n"
            f"Agent: {case_row['agent'] or 'N/A'} from {case_row['call_center'] or 'N/A'} did not follow this process: \"{process}\".\n"
            f"Source: {qw.BEHAVIOR_SOURCE}."
        )
    return qw.alert_text(case_row["itinerary"], case_row["agent"], case_row["call_center"], process)


def _send_issue_if_needed(row, qa):
    settings = qw.read_automation_settings()
    if not settings.get("slack_on_qa_issue", True):
        return False
    with qw.db() as c:
        fresh = c.execute("SELECT * FROM cases WHERE id=?", (row["id"],)).fetchone()
    if not fresh:
        return False
    return _post_slack(row["id"], _issue_text(fresh, qa))


def _send_good_job_if_needed(row):
    with qw.db() as c:
        fresh = c.execute("SELECT * FROM cases WHERE id=?", (row["id"],)).fetchone()
    if not fresh or fresh["slack_alert_sent_at"]:
        return False
    itinerary = fresh["itinerary"] or "N/A"
    agent = fresh["agent"] or "N/A"
    center = fresh["call_center"] or "N/A"
    score = fresh["score"]
    no_booking = int(fresh["booking_found"] or 0) == 0
    result_line = (
        "Behavior review completed with no customer-service conduct issue found."
        if no_booking else
        "QA completed with no issue found."
    )
    if score is not None:
        result_line += f" Score: {score}."
    text = (
        "Good Job\n"
        f"Booking: {itinerary}\n"
        f"Agent: {agent} from {center}\n"
        f"Result: {result_line}"
    )
    return _post_slack(row["id"], text)


def process_every_transcript(row):
    settings = qw.read_automation_settings()
    if not settings["auto_qa_after_transcription"]:
        return

    # 1) No booking: behavior QA is still mandatory.
    if int(row["booking_found"] or 0) == 0:
        qa = qw._conduct_qa_case(row)
        issue, _ = qw._save_qa_result(row, qa, None, no_booking=True)
        if issue:
            _send_issue_if_needed(row, qa)
        else:
            _send_good_job_if_needed(row)
        _delete_audio_after_qa(row)
        return

    # 2) Booking(s) exist: first try to identify exactly one booking.
    qw.write_status(
        state="booking_matching",
        current_call_id=row["call_id"],
        current_agent=row["agent"],
        message=f"Matching booking for {row['call_id']}",
    )
    chosen, confidence, match_status = qw.match_one_booking(row)
    qw._save_match(row, chosen, confidence, match_status)

    # Multiple bookings but no unique match: do NOT QA two bookings.
    # Still run mandatory conduct QA on the transcript, then keep the case in Attention.
    if not chosen:
        qa = qw._conduct_qa_case(row)
        issue, _ = qw._save_qa_result(row, qa, None, no_booking=False)
        if issue:
            _send_issue_if_needed(row, qa)
        with qw.db() as c:
            current = c.execute("SELECT finding,severity FROM cases WHERE id=?", (row["id"],)).fetchone()
            behavior_finding = (current["finding"] or "").strip() if current else ""
            match_finding = "Multiple bookings were found, but one unique booking could not be matched to the call. Booking-specific QA was not run on multiple bookings. Conduct QA was completed."
            combined = match_finding + (("\nBehavior QA: " + behavior_finding) if behavior_finding else "")
            severity = current["severity"] if current and current["severity"] in {"warning", "critical"} else "warning"
            c.execute(
                "UPDATE cases SET status='needs_attention',severity=?,finding=?,updated_at=? WHERE id=?",
                (severity, combined, qw.utc_now(), row["id"]),
            )
        _delete_audio_after_qa(row)
        return

    # 3) Exactly one booking matched: full Matrix/Rubric QA.
    docs = qw.booking_documentation_text(chosen["data"])
    with qw.db() as c:
        c.execute("UPDATE cases SET missing_notes=? WHERE id=?", (0 if docs.strip() else 1, row["id"]))

    qa = qw._qa_case(row, chosen["data"])
    issue, _ = qw._save_qa_result(row, qa, chosen["data"], no_booking=False)
    if issue:
        _send_issue_if_needed(row, qa)
    else:
        _send_good_job_if_needed(row)
    _delete_audio_after_qa(row)


def main():
    # Original worker loop stays in charge of queueing/retries/status.
    # Only the per-call lifecycle is replaced.
    qw.process_case = process_every_transcript
    qw.main()


if __name__ == "__main__":
    main()
