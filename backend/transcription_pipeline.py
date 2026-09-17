from __future__ import annotations

import json
import re
import time
from pathlib import Path

import backend.transcription_worker as tw
from backend.booking_rules import corrected_ingest_json_file, ensure_booking_schema

CALL_ID_RE = re.compile(r"(CA[0-9A-Za-z]{20,})", re.IGNORECASE)


def _looks_like_ai_agent(path: Path) -> bool:
    text = str(path).lower().replace("_", " ").replace("-", " ")
    return "ai agents" in text or "aiagent" in text or "ai agent" in text


def _call_id_from_audio(path: Path) -> str:
    match = CALL_ID_RE.search(path.stem)
    return match.group(1) if match else f"AUDIO-{path.stem}"


def recover_already_downloaded_calls() -> dict:
    """Recover calls already sitting in QA-CALLS before or while QA ALERT was offline.

    JSON-backed calls are ingested with the normal corrected booking rules. If an audio
    file exists without a usable JSON, create a minimal case so the audio still receives
    transcription + behavior QA instead of being stranded on disk.
    """
    tw.ensure_schema()
    ensure_booking_schema()
    tw.INBOX.mkdir(parents=True, exist_ok=True)

    json_seen = 0
    audio_seen = 0
    orphan_cases = 0

    try:
        json_files = sorted(tw.INBOX.rglob("*.json"), key=lambda p: p.stat().st_mtime)
    except Exception:
        json_files = []

    for path in json_files:
        try:
            corrected_ingest_json_file(path)
            json_seen += 1
        except Exception as exc:
            print(f"[RECOVERY] Could not ingest {path.name}: {exc}")

    # Attach all currently available audio to the database rows created/repaired above.
    try:
        tw.reconcile_inbox()
    except Exception as exc:
        print(f"[RECOVERY] Inbox reconciliation warning: {exc}")

    try:
        audio_files = [
            p for p in tw.INBOX.rglob("*")
            if p.is_file() and p.suffix.lower() in tw.AUDIO_EXTENSIONS
        ]
    except Exception:
        audio_files = []

    for audio in audio_files:
        audio_seen += 1
        if _looks_like_ai_agent(audio):
            # Old AI-Agent downloads are intentionally ignored just like the Chrome filter.
            continue

        call_id = _call_id_from_audio(audio)
        try:
            with tw.db() as c:
                row = c.execute("SELECT id FROM cases WHERE call_id=?", (call_id,)).fetchone()
                if row:
                    c.execute(
                        "UPDATE cases SET audio_path=COALESCE(audio_path,?),updated_at=? WHERE id=?",
                        (str(audio.resolve()), tw.utc_now(), row["id"]),
                    )
                    continue

                # No JSON/case exists, but the downloaded human-call audio must not be lost.
                # It will receive behavior-only QA because no booking metadata is available.
                now = tw.utc_now()
                c.execute(
                    """
                    INSERT INTO cases(
                      call_id,itinerary,agent,call_center,caller_number,duration_seconds,
                      status,severity,finding,missing_notes,created_at,updated_at,
                      booking_found,booking_count,booking_match_status,source_json_path,audio_path
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        call_id,
                        "N/A",
                        "Recovered downloaded call",
                        "N/A",
                        "N/A",
                        0,
                        "queued",
                        "info",
                        "Recovered from an already-downloaded audio file. Booking metadata was unavailable; behavior QA will still run.",
                        0,
                        now,
                        now,
                        0,
                        0,
                        "not_found",
                        None,
                        str(audio.resolve()),
                    ),
                )
                orphan_cases += 1
                print(f"[RECOVERY] Added already-downloaded audio to QA queue: {audio.name}")
        except Exception as exc:
            print(f"[RECOVERY] Could not recover {audio.name}: {exc}")

    return {
        "json_seen": json_seen,
        "audio_seen": audio_seen,
        "orphan_cases_created": orphan_cases,
    }


def process_case_after_transcript(model, row):
    """Transcribe every call, save transcript, but keep full audio until QA finishes."""
    audio = tw.find_audio_for_row(row)
    if not audio:
        with tw.db() as c:
            c.execute(
                "UPDATE cases SET status='queued',transcription_error=?,updated_at=? WHERE id=?",
                ("Audio file was not found yet.", tw.utc_now(), row["id"]),
            )
        return

    tw.write_status(
        state="transcribing",
        current_call_id=row["call_id"],
        current_agent=row["agent"],
        audio_path=str(audio),
        started_at=tw.utc_now(),
        message=f"Transcribing {row['call_id']}",
    )
    print(f"[WHISPER] Transcribing {row['call_id']} - {row['agent']}")

    try:
        text, segments, words, language, probability = tw.collect_transcript(model, audio)
        if not text:
            raise RuntimeError("Whisper returned an empty transcript; audio was kept for retry/review.")

        txt_path, json_path = tw.save_transcript(row, text, segments, words, language, probability)
        completed = tw.utc_now()
        with tw.db() as c:
            c.execute(
                """
                UPDATE cases
                SET transcript_text=?,transcript_path=?,transcript_json_path=?,transcript_language=?,
                    transcription_completed_at=?,status='transcribed',transcription_error=NULL,updated_at=?
                WHERE id=?
                """,
                (text, str(txt_path), str(json_path), language, completed, completed, row["id"]),
            )

        print(f"[WHISPER] Completed {row['call_id']} - transcript saved; audio retained until QA finishes")
        tw.write_status(
            state="idle",
            last_completed_call_id=row["call_id"],
            last_completed_at=completed,
            last_audio_deleted=False,
            current_call_id=None,
            current_agent=None,
            audio_path=None,
            message="Transcript saved - waiting for QA before deleting audio",
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        now = tw.utc_now()
        print(f"[WHISPER] ERROR {row['call_id']}: {error}")
        with tw.db() as c:
            c.execute(
                "UPDATE cases SET status='transcription_error',transcription_error=?,updated_at=? WHERE id=?",
                (error, now, row["id"]),
            )
        tw.write_status(
            state="error",
            current_call_id=None,
            current_agent=None,
            last_error=error,
            last_error_call_id=row["call_id"],
            message="Transcription error - full audio was kept",
        )
        time.sleep(5)


def main():
    # Recover anything that was downloaded before QA ALERT started.
    result = recover_already_downloaded_calls()
    print(
        "[RECOVERY] Existing QA-CALLS scan complete: "
        f"{result['json_seen']} JSON, {result['audio_seen']} audio, "
        f"{result['orphan_cases_created']} orphan audio case(s) recovered"
    )

    tw.process_case = process_case_after_transcript

    # Keep rescanning in the background loop too, not only once at startup.
    original_reconcile = tw.reconcile_inbox
    last_recovery = [0.0]

    def reconcile_with_recovery():
        linked = original_reconcile()
        now = time.time()
        if now - last_recovery[0] >= 15:
            last_recovery[0] = now
            try:
                recover_already_downloaded_calls()
            except Exception as exc:
                print(f"[RECOVERY] Periodic recovery warning: {exc}")
        return linked

    tw.reconcile_inbox = reconcile_with_recovery
    tw.run_forever()


if __name__ == "__main__":
    main()
