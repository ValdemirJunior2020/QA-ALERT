from __future__ import annotations

import json
from pathlib import Path

from fastapi import File, HTTPException, UploadFile
from fastapi.responses import FileResponse

import backend.app as app_module
from backend.app import app, db
from backend.booking_rules import (
    corrected_ingest_json_file,
    ensure_booking_schema,
    read_automation_settings,
    write_automation_settings,
)
from backend.knowledge_loader import KNOWLEDGE, source_status
from backend.transcription_worker import STATUS_FILE, TRANSCRIPTS, ensure_schema

ROOT = Path(__file__).resolve().parent.parent
AUTOMATION_HTML = ROOT / "frontend" / "automation.html"
QA_STATUS_FILE = ROOT / "data" / "qa-worker-status.json"

ensure_schema()
ensure_booking_schema()

# Permanent corrected ingest behavior used by the existing inbox watcher.
# scan_inbox_once() in backend.app resolves ingest_json_file dynamically, so
# replacing the module function here also repairs all future automatic scans.
app_module.ingest_json_file = corrected_ingest_json_file


def _worker_file_status():
    try:
        if STATUS_FILE.exists():
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"state": "status_read_error", "error": str(exc)}
    return {"state": "not_started", "message": "Whisper worker has not written a status yet."}


def _qa_worker_status():
    try:
        if QA_STATUS_FILE.exists():
            return json.loads(QA_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"state": "status_read_error", "error": str(exc)}
    return {"state": "not_started", "message": "QA worker has not written a status yet."}


@app.get("/automation")
def automation_page():
    if not AUTOMATION_HTML.exists():
        raise HTTPException(404, "Automation settings page missing")
    return FileResponse(AUTOMATION_HTML)


@app.get("/api/automation-settings")
def get_automation_settings():
    return read_automation_settings()


@app.put("/api/automation-settings")
async def put_automation_settings(payload: dict):
    return write_automation_settings(payload)


@app.get("/api/knowledge/status")
def knowledge_status():
    return source_status()


@app.post("/api/knowledge/upload")
async def knowledge_upload(file: UploadFile = File(...)):
    filename = Path(file.filename or "").name
    ext = Path(filename).suffix.lower()
    if ext not in {".xlsx", ".xlsm", ".docx", ".txt", ".md", ".json"}:
        raise HTTPException(400, "Use XLSX, XLSM, DOCX, TXT, MD, or JSON")
    if not filename:
        raise HTTPException(400, "File name is missing")
    KNOWLEDGE.mkdir(parents=True, exist_ok=True)
    target = KNOWLEDGE / filename
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(400, "Knowledge file is larger than 50 MB")
    target.write_bytes(content)
    return {"ok": True, "saved": filename, "status": source_status()}


@app.get("/api/workers")
def worker_status():
    with db() as c:
        queued = c.execute("SELECT COUNT(*) n FROM cases WHERE status='queued'").fetchone()["n"]
        transcribing = c.execute("SELECT COUNT(*) n FROM cases WHERE status='transcribing'").fetchone()["n"]
        completed = c.execute("SELECT COUNT(*) n FROM cases WHERE status='transcribed_audio_deleted'").fetchone()["n"]
        cleanup_errors = c.execute("SELECT COUNT(*) n FROM cases WHERE status='transcribed_audio_cleanup_error'").fetchone()["n"]
        errors = c.execute("SELECT COUNT(*) n FROM cases WHERE status='transcription_error'").fetchone()["n"]
        qa_running = c.execute("SELECT COUNT(*) n FROM cases WHERE status='qa_running'").fetchone()["n"]
        qa_errors = c.execute("SELECT COUNT(*) n FROM cases WHERE status='qa_error'").fetchone()["n"]
        completed_qa = c.execute("SELECT COUNT(*) n FROM cases WHERE status IN ('completed','completed_no_booking')").fetchone()["n"]
        last = c.execute(
            """
            SELECT id,call_id,agent,call_center,status,transcript_language,
                   transcription_started_at,transcription_completed_at,audio_deleted_at,
                   transcription_error,transcript_path,itinerary,booking_count,booking_match_status,
                   booking_match_confidence,qa_process,qa_matrix_source,slack_alert_sent_at
            FROM cases
            WHERE transcription_started_at IS NOT NULL OR transcript_text IS NOT NULL
            ORDER BY COALESCE(transcription_completed_at,transcription_started_at,updated_at) DESC
            LIMIT 1
            """
        ).fetchone()
    return {
        "whisper": _worker_file_status(),
        "qa": _qa_worker_status(),
        "knowledge": source_status(),
        "counts": {
            "queued": queued,
            "transcribing": transcribing,
            "completed_audio_deleted": completed,
            "cleanup_errors": cleanup_errors,
            "transcription_errors": errors,
            "qa_running": qa_running,
            "qa_errors": qa_errors,
            "completed_qa": completed_qa,
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
                   missing_notes,booking_found,booking_count,booking_match_status,booking_match_confidence,
                   transcript_language,transcription_started_at,transcription_completed_at,audio_deleted_at,
                   transcription_error,qa_process,qa_matrix_source,slack_alert_sent_at,created_at,updated_at
            FROM cases
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"cases": [dict(r) for r in rows]}


@app.get("/api/cases/{case_id}/bookings")
def case_bookings(case_id: int):
    with db() as c:
        case = c.execute("SELECT id,call_id,itinerary,booking_found,booking_count,booking_match_status,booking_match_confidence FROM cases WHERE id=?", (case_id,)).fetchone()
        if not case:
            raise HTTPException(404, "Case not found")
        rows = c.execute("SELECT id,itinerary,fit_id,booking_json_path,documentation_found,is_matched,match_confidence FROM booking_candidates WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
    return {"case": dict(case), "bookings": [dict(r) for r in rows]}


@app.get("/api/cases/{case_id}/transcript")
def case_transcript(case_id: int):
    with db() as c:
        row = c.execute(
            """
            SELECT id,call_id,agent,call_center,status,transcript_text,transcript_language,
                   transcript_path,transcript_json_path,transcription_completed_at,audio_deleted_at,
                   transcription_error,itinerary,booking_count,booking_match_status,qa_process,qa_matrix_source
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
