from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from backend.app import app, db
from backend.transcription_worker import STATUS_FILE, TRANSCRIPTS, ensure_schema

ensure_schema()


def _worker_file_status():
    try:
        if STATUS_FILE.exists():
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"state": "status_read_error", "error": str(exc)}
    return {"state": "not_started", "message": "Whisper worker has not written a status yet."}


@app.get("/api/workers")
def worker_status():
    with db() as c:
        queued = c.execute("SELECT COUNT(*) n FROM cases WHERE status='queued'").fetchone()["n"]
        transcribing = c.execute("SELECT COUNT(*) n FROM cases WHERE status='transcribing'").fetchone()["n"]
        completed = c.execute("SELECT COUNT(*) n FROM cases WHERE status='transcribed_audio_deleted'").fetchone()["n"]
        cleanup_errors = c.execute("SELECT COUNT(*) n FROM cases WHERE status='transcribed_audio_cleanup_error'").fetchone()["n"]
        errors = c.execute("SELECT COUNT(*) n FROM cases WHERE status='transcription_error'").fetchone()["n"]
        last = c.execute(
            """
            SELECT id,call_id,agent,call_center,status,transcript_language,
                   transcription_started_at,transcription_completed_at,audio_deleted_at,
                   transcription_error,transcript_path
            FROM cases
            WHERE transcription_started_at IS NOT NULL OR transcript_text IS NOT NULL
            ORDER BY COALESCE(transcription_completed_at,transcription_started_at,updated_at) DESC
            LIMIT 1
            """
        ).fetchone()
    return {
        "whisper": _worker_file_status(),
        "counts": {
            "queued": queued,
            "transcribing": transcribing,
            "completed_audio_deleted": completed,
            "cleanup_errors": cleanup_errors,
            "transcription_errors": errors,
        },
        "last_case": dict(last) if last else None,
    }


@app.get("/api/cases/recent")
def recent_cases(limit: int = 50):
    limit = max(1, min(200, int(limit)))
    with db() as c:
        rows = c.execute(
            """
            SELECT id,call_id,itinerary,agent,call_center,duration_seconds,status,severity,
                   missing_notes,transcript_language,transcription_started_at,
                   transcription_completed_at,audio_deleted_at,transcription_error,
                   created_at,updated_at
            FROM cases
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"cases": [dict(r) for r in rows]}


@app.get("/api/cases/{case_id}/transcript")
def case_transcript(case_id: int):
    with db() as c:
        row = c.execute(
            """
            SELECT id,call_id,agent,call_center,status,transcript_text,transcript_language,
                   transcript_path,transcript_json_path,transcription_completed_at,audio_deleted_at,
                   transcription_error
            FROM cases WHERE id=?
            """,
            (case_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Case not found")
    return dict(row)


@app.post("/api/cases/{case_id}/transcription/retry")
def retry_transcription(case_id: int):
    with db() as c:
        row = c.execute("SELECT id,status,transcript_text FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Case not found")
        if row["transcript_text"]:
            raise HTTPException(409, "This case already has a saved transcript")
        c.execute(
            "UPDATE cases SET status='queued',transcription_error=NULL,updated_at=datetime('now') WHERE id=?",
            (case_id,),
        )
    return {"ok": True, "case_id": case_id, "status": "queued"}
