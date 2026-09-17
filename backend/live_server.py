from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.export_server import app, db


def _attention_where() -> str:
    # Booking-not-found by itself is NOT an attention condition.
    # No-booking calls may still appear if QA found a real transcript-supported issue
    # (qa_result_json exists and the case status/severity indicates attention).
    return """
      (
        COALESCE(booking_found,0)=1
        AND missing_notes=1
      )
      OR severity='critical'
      OR (
        status='needs_attention'
        AND (
          COALESCE(booking_found,0)=1
          OR COALESCE(qa_result_json,'')<>''
        )
      )
    """


@app.middleware("http")
async def corrected_attention_rules(request: Request, call_next):
    path = request.url.path

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
        return JSONResponse({"cases": [dict(r) for r in rows]})

    if path == "/api/dashboard":
        with db() as c:
            total = c.execute("SELECT COUNT(*) n FROM cases").fetchone()["n"]
            active = c.execute(
                "SELECT COUNT(*) n FROM cases WHERE status IN ('queued','collecting','transcribing','qa_running')"
            ).fetchone()["n"]
            critical = c.execute("SELECT COUNT(*) n FROM cases WHERE severity='critical'").fetchone()["n"]
            missing = c.execute(
                "SELECT COUNT(*) n FROM cases WHERE COALESCE(booking_found,0)=1 AND missing_notes=1"
            ).fetchone()["n"]
            attention = c.execute(
                f"SELECT COUNT(*) n FROM cases WHERE {_attention_where()}"
            ).fetchone()["n"]
        return JSONResponse({
            "total": total,
            "active": active,
            "critical": critical,
            "missing_notes": missing,
            "attention": attention,
        })

    return await call_next(request)


# Repair only the false documentation flag on historical no-booking rows.
# Do NOT erase a real QA finding or a transcript-supported conduct issue.
try:
    with db() as c:
        c.execute(
            """
            UPDATE cases
            SET missing_notes=0,
                severity=CASE WHEN severity='warning' AND COALESCE(qa_result_json,'')='' THEN 'info' ELSE severity END
            WHERE COALESCE(booking_found,0)=0
              AND missing_notes=1
            """
        )
except Exception as exc:
    print(f"[QA ALERT] Historical no-booking attention cleanup warning: {exc}")
