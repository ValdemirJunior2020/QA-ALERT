from __future__ import annotations

from pathlib import Path

import backend.qa_worker as qw

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".webm", ".aac", ".flac"}
INBOX = Path.home() / "Downloads" / "QA-CALLS"


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


def _send_issue_if_needed(row, qa):
    with qw.db() as c:
        fresh = c.execute("SELECT * FROM cases WHERE id=?", (row["id"],)).fetchone()
    if fresh and qw._send_issue_slack(fresh, qa):
        qw._mark_slack_sent(row["id"])


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
    _delete_audio_after_qa(row)


def main():
    # Original worker loop stays in charge of queueing/retries/status.
    # Only the per-call lifecycle is replaced.
    qw.process_case = process_every_transcript
    qw.main()


if __name__ == "__main__":
    main()
