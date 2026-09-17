from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from backend.booking_rules import alert_text, booking_documentation_text, db as booking_db, ensure_booking_schema, read_automation_settings

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = DATA / "qa-alert.db"
KNOWLEDGE = ROOT / "knowledge"
STATUS_FILE = DATA / "qa-worker-status.json"
RESULTS = DATA / "qa-results"
RESULTS.mkdir(parents=True, exist_ok=True)
KNOWLEDGE.mkdir(parents=True, exist_ok=True)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
POLL_SECONDS = max(2, int(os.getenv("QA_WORKER_POLL_SECONDS", "4")))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def write_status(**patch):
    current = {}
    try:
        if STATUS_FILE.exists():
            current = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    current.update(patch)
    current["updated_at"] = utc_now()
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS_FILE)


def _ollama_json(system: str, payload: dict) -> dict:
    body = {
        "model": OLLAMA_MODEL,
        "format": "json",
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "options": {"temperature": 0.1},
    }
    r = requests.post(f"{OLLAMA_BASE_URL}/api/chat", json=body, timeout=180)
    r.raise_for_status()
    content = r.json().get("message", {}).get("content", "{}")
    return json.loads(content)


def _knowledge_text() -> str:
    chunks: list[str] = []
    for p in sorted(KNOWLEDGE.rglob("*")):
        if not p.is_file():
            continue
        try:
            if p.suffix.lower() in {".txt", ".md", ".json"}:
                text = p.read_text(encoding="utf-8", errors="ignore")
                chunks.append(f"\n--- {p.name} ---\n{text[:100000]}")
        except Exception:
            continue
    return "\n".join(chunks)


def _booking_candidates(case_id: int) -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM booking_candidates WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
    out = []
    for r in rows:
        try:
            data = json.loads(Path(r["booking_json_path"]).read_text(encoding="utf-8"))
        except Exception:
            data = {}
        out.append({"row": dict(r), "data": data})
    return out


def match_one_booking(case_row: sqlite3.Row) -> tuple[dict | None, float, str]:
    candidates = _booking_candidates(case_row["id"])
    if not candidates:
        return None, 0.0, "not_found"
    if len(candidates) == 1:
        return candidates[0], 1.0, "matched"

    compact = []
    for c in candidates:
        d = c["data"]
        compact.append({
            "itinerary": c["row"].get("itinerary") if isinstance(c["row"], dict) else c["row"]["itinerary"],
            "booking": d,
        })

    system = (
        "You match one hotel customer-service call transcript to exactly one booking. "
        "Use concrete evidence from the transcript such as itinerary/confirmation number, hotel, guest, dates, room type, amount, cancellation context, or other unique facts. "
        "Return JSON only: {matched_itinerary:string|null, confidence:number 0..1, reason:string}. "
        "If one unique booking cannot be established, matched_itinerary must be null. Never choose two bookings."
    )
    result = _ollama_json(system, {"transcript": case_row["transcript_text"], "bookings": compact})
    itinerary = result.get("matched_itinerary")
    confidence = float(result.get("confidence") or 0.0)
    if not itinerary:
        return None, confidence, "ambiguous"
    chosen = next((c for c in candidates if str(c["row"]["itinerary"]).upper() == str(itinerary).upper()), None)
    if not chosen:
        return None, confidence, "ambiguous"
    min_conf = read_automation_settings()["booking_match_min_confidence"]
    if confidence < min_conf:
        return None, confidence, "low_confidence"
    return chosen, confidence, "matched"


def _save_match(case_row, chosen, confidence: float, status: str):
    now = utc_now()
    with db() as c:
        c.execute("UPDATE booking_candidates SET is_matched=0,match_confidence=NULL WHERE case_id=?", (case_row["id"],))
        if chosen:
            c.execute("UPDATE booking_candidates SET is_matched=1,match_confidence=? WHERE id=?", (confidence, chosen["row"]["id"]))
            c.execute("""
                UPDATE cases SET itinerary=?,matched_booking_path=?,booking_match_status=?,booking_match_confidence=?,updated_at=? WHERE id=?
            """, (chosen["row"]["itinerary"], chosen["row"]["booking_json_path"], status, confidence, now, case_row["id"]))
        else:
            c.execute("UPDATE cases SET booking_match_status=?,booking_match_confidence=?,status='needs_attention',finding=?,updated_at=? WHERE id=?",
                      (status, confidence, "Multiple bookings were found, but one unique booking could not be matched to the call. QA was not run on multiple bookings.", now, case_row["id"]))


def _qa_case(case_row: sqlite3.Row, booking: dict) -> dict:
    write_status(state="matrix_check", current_call_id=case_row["call_id"], current_agent=case_row["agent"], message=f"Checking Matrix sources for {case_row['call_id']}")
    knowledge = _knowledge_text()
    if not knowledge.strip():
        raise RuntimeError("QA knowledge is empty. Put the QA Form/Matrix/update source material in the knowledge folder.")

    system = (
        "You are a strict hotel call QA engine. Evaluate ONLY the single matched booking provided. "
        "The QA Form/Rubric controls scoring. Process-update material overrides older Matrix instructions when conflicting. "
        "Never invent a requirement. If the evidence is insufficient, do not fail the agent. "
        "Return JSON only with: issue_found(bool), score(number|null), severity(info|warning|critical), guest_request, agent_action, process, matrix_source, finding, evidence_excerpt."
    )
    write_status(state="ollama_qa", current_call_id=case_row["call_id"], current_agent=case_row["agent"], message=f"Running Ollama QA for {case_row['call_id']}")
    return _ollama_json(system, {
        "transcript": case_row["transcript_text"],
        "booking": booking,
        "source_of_truth": knowledge,
    })


def _send_issue_slack(case_row, qa: dict):
    settings = read_automation_settings()
    if not settings["slack_on_qa_issue"]:
        return False
    with db() as c:
        raw = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    if raw.get("slack_enabled", "false") != "true":
        return False
    recipient = raw.get("slack_recipient_id", "").strip()
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    if not recipient or not token:
        return False

    text = alert_text(case_row["itinerary"], case_row["agent"], case_row["call_center"], qa.get("process") or "Unspecified process")
    from slack_sdk import WebClient
    client = WebClient(token=token)
    channel = recipient
    if recipient.startswith(("U", "W")):
        channel = client.conversations_open(users=[recipient])["channel"]["id"]
    client.chat_postMessage(channel=channel, text=text)
    return True


def process_case(row: sqlite3.Row):
    settings = read_automation_settings()
    if not settings["auto_qa_after_transcription"]:
        return

    # Booking not found: save transcript, but do NOT create missing-documentation alerts.
    if int(row["booking_found"] or 0) == 0:
        with db() as c:
            c.execute("UPDATE cases SET missing_notes=0,severity='info',finding=NULL,status='completed_no_booking',updated_at=? WHERE id=?", (utc_now(), row["id"]))
        return

    write_status(state="booking_matching", current_call_id=row["call_id"], current_agent=row["agent"], message=f"Matching booking for {row['call_id']}")
    chosen, confidence, match_status = match_one_booking(row)
    _save_match(row, chosen, confidence, match_status)
    if not chosen:
        return

    docs = booking_documentation_text(chosen["data"])
    # Documentation is evaluated only on the ONE matched booking.
    with db() as c:
        c.execute("UPDATE cases SET missing_notes=? WHERE id=?", (0 if docs.strip() else 1, row["id"]))

    qa = _qa_case(row, chosen["data"])
    now = utc_now()
    result_path = RESULTS / f"{row['call_id']}.json"
    result_path.write_text(json.dumps({"call_id": row["call_id"], "matched_booking": chosen["data"], "qa": qa}, ensure_ascii=False, indent=2), encoding="utf-8")

    issue = bool(qa.get("issue_found"))
    severity = str(qa.get("severity") or ("warning" if issue else "info"))
    finding = str(qa.get("finding") or "") or None
    with db() as c:
        c.execute("""
            UPDATE cases SET guest_request=?,agent_action=?,score=?,severity=?,finding=?,qa_process=?,qa_matrix_source=?,qa_result_json=?,
                evidence_excerpt=?,status=?,updated_at=? WHERE id=?
        """, (
            qa.get("guest_request"), qa.get("agent_action"), qa.get("score"), severity, finding,
            qa.get("process"), qa.get("matrix_source"), str(result_path), qa.get("evidence_excerpt"),
            "needs_attention" if issue else "completed", now, row["id"]
        ))

    if issue:
        with db() as c:
            fresh = c.execute("SELECT * FROM cases WHERE id=?", (row["id"],)).fetchone()
        if _send_issue_slack(fresh, qa):
            with db() as c:
                c.execute("UPDATE cases SET slack_alert_sent_at=?,updated_at=? WHERE id=?", (utc_now(), utc_now(), row["id"]))


def next_case():
    ensure_booking_schema()
    with db() as c:
        return c.execute("""
            SELECT * FROM cases
            WHERE status IN ('transcribed_audio_deleted','transcribed')
              AND COALESCE(qa_result_json,'')=''
            ORDER BY COALESCE(transcription_completed_at,updated_at) ASC
            LIMIT 1
        """).fetchone()


def main():
    ensure_booking_schema()
    write_status(state="idle", message="QA worker ready", model=OLLAMA_MODEL, last_completed_call_id=None, last_completed_at=None)
    while True:
        try:
            row = next_case()
            if not row:
                write_status(state="idle", message="Waiting for transcribed calls")
                time.sleep(POLL_SECONDS)
                continue
            write_status(state="qa_running", current_call_id=row["call_id"], current_agent=row["agent"], message=f"Matching booking and QA'ing {row['call_id']}")
            try:
                process_case(row)
                with db() as c:
                    finished = c.execute("SELECT status FROM cases WHERE id=?", (row["id"],)).fetchone()
                actual_completed = bool(finished and finished["status"] == "completed")
                write_status(
                    state="idle",
                    last_completed_call_id=row["call_id"] if actual_completed else None,
                    last_completed_at=utc_now() if actual_completed else None,
                    current_call_id=None,
                    current_agent=None,
                    message="QA completed" if actual_completed else "QA cycle finished without a completed QA",
                )
            except Exception as exc:
                with db() as c:
                    c.execute("UPDATE cases SET status='qa_error',finding=?,updated_at=? WHERE id=?", (f"QA worker error: {exc}", utc_now(), row["id"]))
                write_status(state="error", current_call_id=row["call_id"], error=str(exc), message="QA worker error")
                time.sleep(POLL_SECONDS)
        except Exception as exc:
            write_status(state="error", error=str(exc), message="QA worker loop error")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
