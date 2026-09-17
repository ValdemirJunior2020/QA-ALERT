from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = DATA / "qa-alert.db"
INBOX = Path.home() / "Downloads" / "QA-CALLS"
BOOKINGS_DIR = DATA / "bookings"
BOOKINGS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_AUTOMATION = {
    "suppress_missing_docs_when_booking_not_found": "true",
    "save_all_booking_json": "true",
    "qa_only_one_matched_booking": "true",
    "multi_booking_no_match_action": "needs_attention",
    "booking_match_min_confidence": "0.75",
    "auto_qa_after_transcription": "true",
    "slack_on_qa_issue": "true",
    "slack_alert_template": "Attention\nBooking: {itinerary}\nAgent: {agent} from {call_center} did not follow this process: \"{process}\" from the Matrix.",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def ensure_booking_schema() -> None:
    DATA.mkdir(exist_ok=True)
    with db() as c:
        existing = {r["name"] for r in c.execute("PRAGMA table_info(cases)").fetchall()}
        additions = {
            "booking_found": "INTEGER DEFAULT 0",
            "booking_count": "INTEGER DEFAULT 0",
            "booking_match_status": "TEXT",
            "booking_match_confidence": "REAL",
            "matched_booking_path": "TEXT",
            "source_json_path": "TEXT",
            "qa_process": "TEXT",
            "qa_matrix_source": "TEXT",
            "qa_result_json": "TEXT",
            "slack_alert_sent_at": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in existing:
                c.execute(f"ALTER TABLE cases ADD COLUMN {name} {sql_type}")
        c.execute("""
            CREATE TABLE IF NOT EXISTS booking_candidates(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              case_id INTEGER NOT NULL,
              call_id TEXT NOT NULL,
              itinerary TEXT,
              fit_id TEXT,
              booking_json_path TEXT NOT NULL,
              documentation_found INTEGER NOT NULL DEFAULT 0,
              is_matched INTEGER NOT NULL DEFAULT 0,
              match_confidence REAL,
              created_at TEXT NOT NULL,
              UNIQUE(call_id, booking_json_path),
              FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
            )
        """)
        for k, v in DEFAULT_AUTOMATION.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (k, v))


def read_automation_settings() -> dict:
    ensure_booking_schema()
    with db() as c:
        raw = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    return {
        "suppress_missing_docs_when_booking_not_found": raw.get("suppress_missing_docs_when_booking_not_found", "true") == "true",
        "save_all_booking_json": raw.get("save_all_booking_json", "true") == "true",
        "qa_only_one_matched_booking": raw.get("qa_only_one_matched_booking", "true") == "true",
        "multi_booking_no_match_action": raw.get("multi_booking_no_match_action", "needs_attention"),
        "booking_match_min_confidence": float(raw.get("booking_match_min_confidence", "0.75")),
        "auto_qa_after_transcription": raw.get("auto_qa_after_transcription", "true") == "true",
        "slack_on_qa_issue": raw.get("slack_on_qa_issue", "true") == "true",
        "slack_alert_template": raw.get("slack_alert_template", DEFAULT_AUTOMATION["slack_alert_template"]),
    }


def write_automation_settings(values: dict) -> dict:
    allowed = set(DEFAULT_AUTOMATION)
    with db() as c:
        for key, value in values.items():
            if key not in allowed:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            c.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    return read_automation_settings()


def _first(data: dict, *keys, default=None):
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def _as_int(value, default=0):
    try:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if ":" in text:
            parts = [int(p) for p in text.split(":")]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return int(float(text))
    except Exception:
        return default


def booking_documentation_text(booking: dict) -> str:
    chunks: list[str] = []
    def walk(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                if any(word in str(k).lower() for word in ("documentation", "notes", "note", "comment", "history", "ticket", "activity", "log")):
                    if isinstance(v, (str, int, float)) and str(v).strip():
                        chunks.append(str(v).strip())
                    else:
                        walk(v, str(k))
                elif isinstance(v, (dict, list)):
                    walk(v, str(k))
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif key and any(word in key.lower() for word in ("documentation", "notes", "note", "comment", "history")):
            text = str(value).strip()
            if text:
                chunks.append(text)
    walk(booking)
    return "\n".join(dict.fromkeys(chunks))


def extract_bookings(booking_lookup: dict) -> list[dict]:
    bookings = booking_lookup.get("bookings")
    if isinstance(bookings, list):
        return [b for b in bookings if isinstance(b, dict)]
    if booking_lookup.get("found") or booking_lookup.get("itinerary"):
        return [booking_lookup]
    return []


def itinerary_for(booking: dict) -> str | None:
    for obj in (booking, booking.get("details") if isinstance(booking.get("details"), dict) else {}, booking.get("reservation_details") if isinstance(booking.get("reservation_details"), dict) else {}):
        if not isinstance(obj, dict):
            continue
        val = obj.get("itinerary")
        if val:
            return str(val).upper()
        fit = obj.get("fit_id") or obj.get("fitId")
        if fit:
            text = str(fit).strip()
            return text.upper() if text.upper().startswith("H") else f"H{text}"
    return None


def save_booking_candidates(case_id: int, call_id: str, bookings: list[dict]) -> list[dict]:
    call_dir = BOOKINGS_DIR / call_id
    call_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    with db() as c:
        c.execute("DELETE FROM booking_candidates WHERE case_id=?", (case_id,))
        for i, booking in enumerate(bookings, 1):
            itinerary = itinerary_for(booking) or f"BOOKING-{i}"
            filename = f"{i:02d}-{''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in itinerary)}.json"
            path = call_dir / filename
            path.write_text(json.dumps(booking, ensure_ascii=False, indent=2), encoding="utf-8")
            docs = booking_documentation_text(booking)
            fit_id = booking.get("fit_id") or (booking.get("details") or {}).get("fit_id") if isinstance(booking.get("details") or {}, dict) else None
            c.execute("""
                INSERT INTO booking_candidates(case_id,call_id,itinerary,fit_id,booking_json_path,documentation_found,created_at)
                VALUES (?,?,?,?,?,?,?)
            """, (case_id, call_id, itinerary, str(fit_id) if fit_id else None, str(path), 1 if docs else 0, utc_now()))
            saved.append({"itinerary": itinerary, "path": str(path), "documentation_found": bool(docs)})
    return saved


def corrected_ingest_json_file(path: Path) -> bool:
    ensure_booking_schema()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return False
    except Exception as exc:
        print(f"[QA ALERT] Could not read {path.name}: {exc}")
        return False

    call_id = str(_first(data, "call_id", "callId", "id", default=f"FILE-{path.stem}")).strip()
    agent = str(_first(data, "agent_name", "agent", default="N/A") or "N/A")
    center = str(_first(data, "call_center", "center", default="N/A") or "N/A")
    caller = str(_first(data, "caller_number", "phone", "caller", default="N/A") or "N/A")
    duration = _as_int(_first(data, "duration_seconds", "call_length_seconds", "duration", "call_length", default=0))
    lookup = data.get("booking_lookup") if isinstance(data.get("booking_lookup"), dict) else {}
    bookings = extract_bookings(lookup)
    booking_found = bool(bookings) or bool(lookup.get("found"))
    settings = read_automation_settings()

    itinerary = itinerary_for(bookings[0]) if len(bookings) == 1 else None
    top_docs = _first(data, "documentation", "notes", "booking_notes", default=None)
    one_booking_docs = booking_documentation_text(bookings[0]) if len(bookings) == 1 else ""
    docs_present = bool(str(top_docs or "").strip() or one_booking_docs.strip())

    # IMPORTANT BUSINESS RULE: if no booking exists, documentation cannot be judged missing.
    if not booking_found and settings["suppress_missing_docs_when_booking_not_found"]:
        missing_notes = 0
        finding = None
        severity = "info"
    elif len(bookings) > 1:
        # Documentation belongs to the booking we eventually match. Do not alert before matching.
        missing_notes = 0
        finding = None
        severity = "info"
    else:
        missing_notes = 0 if docs_present else 1
        finding = "No documentation/notes were found for the matched booking." if missing_notes else None
        severity = "warning" if missing_notes else "info"

    now = utc_now()
    with db() as c:
        existing = c.execute("SELECT * FROM cases WHERE call_id=?", (call_id,)).fetchone()
        if existing:
            case_id = existing["id"]
            # Repair prior false missing-documentation flags when booking was not found.
            c.execute("""
                UPDATE cases SET booking_found=?,booking_count=?,source_json_path=?,
                    missing_notes=?,finding=?,severity=?,
                    itinerary=CASE WHEN ? IS NOT NULL THEN ? ELSE itinerary END,updated_at=?
                WHERE id=?
            """, (1 if booking_found else 0, len(bookings), str(path.resolve()), missing_notes, finding, severity,
                  itinerary, itinerary, now, case_id))
            created = False
        else:
            cur = c.execute("""
                INSERT INTO cases(call_id,itinerary,agent,call_center,caller_number,duration_seconds,
                    status,severity,finding,missing_notes,created_at,updated_at,
                    booking_found,booking_count,booking_match_status,source_json_path)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (call_id, itinerary or "N/A", agent, center, caller, duration,
                  "queued", severity, finding, missing_notes, now, now,
                  1 if booking_found else 0, len(bookings),
                  "not_found" if not booking_found else ("matched" if len(bookings) == 1 else "pending"),
                  str(path.resolve())))
            case_id = cur.lastrowid
            created = True

    if settings["save_all_booking_json"] and bookings:
        saved = save_booking_candidates(case_id, call_id, bookings)
        if len(saved) == 1:
            with db() as c:
                c.execute("UPDATE booking_candidates SET is_matched=1,match_confidence=1.0 WHERE case_id=?", (case_id,))
                c.execute("UPDATE cases SET matched_booking_path=?,booking_match_confidence=1.0 WHERE id=?", (saved[0]["path"], case_id))

    if created:
        print(f"[QA ALERT] Imported call {call_id} ({len(bookings)} booking(s)) from {path.name}")
    return created


def alert_text(itinerary: str, agent: str, call_center: str, process: str) -> str:
    template = read_automation_settings()["slack_alert_template"]
    return template.format(
        itinerary=itinerary or "N/A",
        agent=agent or "N/A",
        call_center=call_center or "N/A",
        process=process or "Unspecified process",
    )
