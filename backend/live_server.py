from __future__ import annotations

import os

from fastapi import Request
from fastapi.responses import JSONResponse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from backend.export_server import app, db


def _ensure_system_attention_schema() -> None:
    # No extra table is required; system errors are identified by the explicit
    # "System / Slack Error:" marker in finding so they remain visible in the
    # same Manager Attention Inbox without becoming agent QA failures.
    pass


def _cleanup_false_no_booking_attention() -> None:
    """Repair legacy no-booking attention rows without hiding real conduct issues."""
    with db() as c:
        c.execute(
            """
            UPDATE cases
            SET severity='info',
                status='needs_attention',
                finding='System / Slack Error: ' ||
                    CASE
                      WHEN LOWER(COALESCE(finding,'')) LIKE '%invalid_auth%' THEN 'Slack authentication failed (invalid_auth). Update the Slack bot token in .env, then restart QA ALERT.'
                      ELSE COALESCE(finding,'Slack notification failed.')
                    END,
                updated_at=datetime('now')
            WHERE (
                LOWER(COALESCE(finding,'')) LIKE '%slack api%'
                OR LOWER(COALESCE(finding,'')) LIKE '%invalid_auth%'
                OR LOWER(COALESCE(finding,'')) LIKE '%slack delivery failed%'
            )
              AND LOWER(COALESCE(finding,'')) NOT LIKE 'system / slack error:%'
            """
        )

        c.execute(
            """
            UPDATE cases
            SET missing_notes=0,
                status=CASE
                    WHEN COALESCE(qa_result_json,'')<>'' THEN 'completed_no_booking'
                    WHEN COALESCE(transcript_text,'')<>'' THEN 'transcribed'
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
              AND LOWER(COALESCE(finding,'')) NOT LIKE 'system / slack error:%'
            """
        )

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
                         AND COALESCE(transcript_text,'')<>'' THEN 'transcribed'
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
              AND LOWER(COALESCE(finding,'')) NOT LIKE 'system / slack error:%'
            """
        )


def _attention_where() -> str:
    return """
      LOWER(COALESCE(finding,'')) LIKE 'system / slack error:%'
      OR (
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


@app.get("/api/slack/status")
def slack_status():
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    with db() as c:
        raw = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    recipient = raw.get("slack_recipient_id", "").strip()
    enabled = raw.get("slack_enabled", "false") == "true"
    if not token:
        return {
            "ok": False,
            "enabled": enabled,
            "token_configured": False,
            "recipient_id": recipient,
            "error": "missing_token",
            "message": "SLACK_BOT_TOKEN is missing from .env",
        }
    try:
        result = WebClient(token=token).auth_test()
        return {
            "ok": True,
            "enabled": enabled,
            "token_configured": True,
            "recipient_id": recipient,
            "team": result.get("team"),
            "user": result.get("user"),
            "bot_id": result.get("bot_id"),
            "message": "Slack token is valid.",
        }
    except SlackApiError as exc:
        error = exc.response.get("error", "unknown_error")
        return {
            "ok": False,
            "enabled": enabled,
            "token_configured": True,
            "recipient_id": recipient,
            "error": error,
            "message": f"Slack authentication failed: {error}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "enabled": enabled,
            "token_configured": True,
            "recipient_id": recipient,
            "error": type(exc).__name__,
            "message": str(exc),
        }


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
                       qa_result_json,qa_process,qa_matrix_source,created_at,updated_at,
                       CASE WHEN LOWER(COALESCE(finding,'')) LIKE 'system / slack error:%'
                            THEN 1 ELSE 0 END AS system_alert
                FROM cases
                WHERE {_attention_where()}
                ORDER BY system_alert DESC,
                         CASE WHEN severity='critical' THEN 0 ELSE 1 END,
                         updated_at DESC
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
                  'booking_matching','matrix_check','ollama_qa','transcribed'
                )
                """
            ).fetchone()["n"]
            critical = c.execute(
                """
                SELECT COUNT(*) n FROM cases
                WHERE severity='critical'
                  AND LOWER(COALESCE(finding,'')) NOT LIKE 'system / slack error:%'
                """
            ).fetchone()["n"]
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
    _ensure_system_attention_schema()
    _cleanup_false_no_booking_attention()
