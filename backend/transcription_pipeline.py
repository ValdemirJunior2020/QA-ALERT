from __future__ import annotations

import time

import backend.transcription_worker as tw


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
    tw.process_case = process_case_after_transcript
    tw.run_forever()


if __name__ == "__main__":
    main()
