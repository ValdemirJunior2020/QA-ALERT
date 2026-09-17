from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.export_server import app, db


def _cleanup_false_no_booking_attention() -> None:
    """Repair legacy no-booking attention rows without hiding real conduct issues.

    Rules:
    - Booking not found is never, by itself, an attention condition.
    - A no-booking call may still need attention only when transcript QA found a
      real warning/critical conduct issue.
    - Legacy INFO rows that still say needs_attention are cleared automatically.
    """
    with db() as c:
        # Old no-booking rows with INFO severity are false attention rows.
        # If a QA result exists, they are clean behavior-QA completions.
        c.execute(
            """
            UPDATE cases
            SET missing_notes=0,
                status=CASE
                    WHEN COALESCE(qa_result_json,'')<>'' THEN 'completed_no_booking'
                    WHEN COALESCE(transcript_text,'')<>'' THEN 'transcribed_audio_deleted'
                    ELSE 'queued'
                END,
                finding=CASE
                    WHEN LOWER(COALESCE(finding,'')) LIKE '%documentation%'
                      OR LOWER(COALESCE(finding,'')) LIKE '%booking details were not available%'
                      OR LOWER(COALESCE(finding,'')) LIKE '%needs manager review%'
                    THEN NULL
                    ELSE finding
                END,
                updated_at=datetime('now')
            WHERE COALESCE(booking_found,0)=0
              AND status='needs_attention'
              AND LOWER(COALESCE(severity,'info'))='info'
            """
        )

        # Repair historical false missing-documentation flags when no booking existed.
        c.execute(
            """
            UPDATE cases
            SET missing_notes=0,
                severity=CASE
                    WHEN severity='warning' AND COALESCE(qa_result_json,'')='' THEN 'info'
                    ELSE severity
                END,
                status=CASE
                    WHEN status='needs_attention' AND COALESCE(qa_result_json,'')=''
                         AND COALESCE(transcript_text,'')<>'' THEN 'transcribed_audio_deleted'
                    WHEN status='needs_attention' AND COALESCE(qa_result_json,'')='' THEN 'queued'
                    ELSE status
                END,
                finding=CASE
                    WHEN COALESCE(qa_result_json,'')='' AND (
                        LOWER(COALESCE(finding,'')) LIKE '%documentation%'
                        OR LOWER(COALESCE(finding,'')) LIKE '%booking details were not available%'
                        OR LOWER(COALESCE(finding,'')) LIKE '%needs manager review%'
                    ) THEN NULL
                    ELSE finding
                END,
                updated_at=datetime('now')
            WHERE COALESCE(booking_found,0)=0
              AND missing_notes=1
            """
        )


def _attention_where() -> str:
    # Booking-not-found by itself is NOT an attention condition.
    # For no-booking calls, only a real QA warning/critical result qualifies.
    return """
      (
        COALESCE(booking_found,0)=1
        AND missing_notes=1
      )
      OR severity='critical'
      OR (
        status='needs_attention'
        AND COALESCE(booking_found,0)=1
      )
      OR (
        status='needs_attention'
        AND COALESCE(booking_found,0)=0
        AND COALESCE(qa_result_json,'')<>''
        AND LOWER(COALESCE(severity,'info')) IN ('warning','critical')
      )
    """


@app.middleware("http")
async def corrected_attention_rules(request: Request, call_next):
    path = request.url.path

    if path in {"/api/cases/attention", "/api/dashboard"}:
        _cleanup_false_no_booking_attention()

    if path == "/api/cases/attention":
        with db() as c:
            rows = c.execute(
                f"""
                SELECT id,call_id,itinerary,agent,call_center,caller_number,duration_seconds,
                       status,severity,finding,missing_notes,booking_found,booking_count,
                       qa_result_json,qa_process,qa_matrix_source,created_at,updated_at
                FROM cases
                WHERE {_attention_where()}
                ORDER BY CASE WHEN severity='critical' THEN 0 ELSE 1 END, updated_at DESC
                """
            ).fetchall()
        return JSONResponse(
            {"cases": [dict(r) for r in rows]},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    if path == "/api/dashboard":
        with db() as c:
            total = c.execute("SELECT COUNT(*) n FROM cases").fetchone()["n"]
            active = c.execute(
                """
                SELECT COUNT(*) n FROM cases
                WHERE status IN (
                  'queued','collecting','transcribing','qa_running',
                  'booking_matching','matrix_check','ollama_qa'
                )
                """
            ).fetchone()["n"]
            critical = c.execute("SELECT COUNT(*) n FROM cases WHERE severity='critical'").fetchone()["n"]
            missing = c.execute(
                "SELECT COUNT(*) n FROM cases WHERE COALESCE(booking_found,0)=1 AND missing_notes=1"
            ).fetchone()["n"]
            attention = c.execute(
                f"SELECT COUNT(*) n FROM cases WHERE {_attention_where()}"
            ).fetchone()["n"]
        return JSONResponse(
            {
                "total": int(total),
                "active": int(active),
                "critical": int(critical),
                "missing_notes": int(missing),
                "attention": int(attention),
            },
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    return await call_next(request)


@app.on_event("startup")
def cleanup_legacy_false_attention_on_startup():
    _cleanup_false_no_booking_attention()
