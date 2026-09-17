from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from openpyxl import Workbook
from pydantic import BaseModel
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
EXPORTS = DATA / "exports"
DB = DATA / "qa-alert.db"
FRONTEND = ROOT / "frontend" / "index.html"
INBOX = Path.home() / "Downloads" / "QA-CALLS"
COOKIE_NAME = "qa_alert_session"
SESSION_HOURS = 12
REMEMBER_DAYS = 30
PBKDF2_ROUNDS = 240_000

DATA.mkdir(exist_ok=True)
EXPORTS.mkdir(exist_ok=True)
INBOX.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="QA ALERT", version="0.4.0")

DEFAULTS = {
    "scan_interval_minutes": "5",
    "minimum_call_seconds": "60",
    "max_active_calls": "5",
    "transcription_workers": "1",
    "ollama_workers": "2",
    "slack_enabled": "false",
    "slack_recipient_name": "Valdemir Gonçalves Junior",
    "slack_recipient_id": "",
    "evidence_padding_before_seconds": "8",
    "evidence_padding_after_seconds": "8",
}


def db():
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          call_id TEXT UNIQUE NOT NULL,
          itinerary TEXT, agent TEXT, call_center TEXT, caller_number TEXT,
          duration_seconds INTEGER DEFAULT 0,
          guest_request TEXT, agent_action TEXT, score REAL,
          status TEXT NOT NULL DEFAULT 'queued', severity TEXT NOT NULL DEFAULT 'info',
          finding TEXT, missing_notes INTEGER NOT NULL DEFAULT 0,
          evidence_start_sec REAL, evidence_end_sec REAL,
          evidence_excerpt TEXT, evidence_path TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status);
        CREATE TABLE IF NOT EXISTS case_reviews(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          case_id INTEGER NOT NULL,
          action TEXT NOT NULL,
          manager_note TEXT,
          correct_procedure TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(case_id) REFERENCES cases(id)
        );
        CREATE INDEX IF NOT EXISTS idx_case_reviews_case_id ON case_reviews(case_id);
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT UNIQUE NOT NULL COLLATE NOCASE,
          password_hash TEXT NOT NULL,
          salt TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL,
          expires_at REAL NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at);
        """)
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (k, v))


init_db()


class Settings(BaseModel):
    scan_interval_minutes: int = 5
    minimum_call_seconds: int = 60
    max_active_calls: int = 5
    transcription_workers: int = 1
    ollama_workers: int = 2
    slack_enabled: bool = False
    slack_recipient_name: str = "Valdemir Gonçalves Junior"
    slack_recipient_id: str = ""
    evidence_padding_before_seconds: int = 8
    evidence_padding_after_seconds: int = 8


class SlackTest(BaseModel):
    recipient_id: Optional[str] = None
    recipient_name: Optional[str] = None


class AttentionResolution(BaseModel):
    action: str
    manager_note: str = ""
    correct_procedure: str = ""


class AuthSetup(BaseModel):
    username: str
    password: str


class AuthLogin(BaseModel):
    username: str
    password: str
    remember: bool = False


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def password_digest(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ROUNDS
    ).hex()


def user_count() -> int:
    with db() as c:
        return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]


def current_user_from_request(request: Request):
    token = request.cookies.get(COOKIE_NAME, "").strip()
    if not token:
        return None
    now = time.time()
    with db() as c:
        c.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        row = c.execute(
            """
            SELECT u.id,u.username,s.expires_at
            FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token=? AND s.expires_at>?
            """,
            (token, now),
        ).fetchone()
    return dict(row) if row else None


PUBLIC_API = {
    "/api/health",
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_API:
        if not current_user_from_request(request):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return await call_next(request)


def read_settings():
    with db() as c:
        raw = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    return {
        "scan_interval_minutes": int(raw.get("scan_interval_minutes", 5)),
        "minimum_call_seconds": int(raw.get("minimum_call_seconds", 60)),
        "max_active_calls": int(raw.get("max_active_calls", 5)),
        "transcription_workers": int(raw.get("transcription_workers", 1)),
        "ollama_workers": int(raw.get("ollama_workers", 2)),
        "slack_enabled": raw.get("slack_enabled", "false") == "true",
        "slack_recipient_name": raw.get("slack_recipient_name", ""),
        "slack_recipient_id": raw.get("slack_recipient_id", ""),
        "evidence_padding_before_seconds": int(raw.get("evidence_padding_before_seconds", 8)),
        "evidence_padding_after_seconds": int(raw.get("evidence_padding_after_seconds", 8)),
    }


def _first(data, *keys, default=None):
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


def ingest_json_file(path: Path):
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[QA ALERT] Could not read {path.name}: {exc}")
        return False

    call_id = str(_first(data, "call_id", "callId", "id", default="")).strip()
    if not call_id:
        call_id = f"FILE-{path.stem}"

    agent = str(_first(data, "agent_name", "agent", default="N/A") or "N/A")
    center = str(_first(data, "call_center", "center", default="N/A") or "N/A")
    caller = str(_first(data, "caller_number", "phone", "caller", default="N/A") or "N/A")
    duration = _as_int(_first(data, "duration_seconds", "call_length_seconds", "duration", "call_length", default=0))

    booking = data.get("booking_lookup") or {}
    if not isinstance(booking, dict):
        booking = {}
    itinerary = _first(data, "itinerary", default=None) or _first(booking, "itinerary", default=None) or "N/A"

    docs = _first(data, "documentation", "notes", "booking_notes", default=None)
    if not docs and isinstance(booking, dict):
        docs = _first(booking, "documentation", "notes", "booking_notes", default=None)
    if isinstance(docs, list):
        docs = "\n".join(str(x) for x in docs if x)
    missing_notes = 0 if (docs and str(docs).strip()) else 1

    now = utc_now()
    finding = None
    if missing_notes:
        finding = "Call imported from QA-CALLS. No documentation/notes were found in the collector JSON."
    elif itinerary == "N/A":
        finding = "Call imported from QA-CALLS. Booking details were not available from the collector."

    with db() as c:
        existing = c.execute("SELECT id FROM cases WHERE call_id=?", (call_id,)).fetchone()
        if existing:
            return False
        c.execute(
            """
            INSERT INTO cases(
              call_id,itinerary,agent,call_center,caller_number,duration_seconds,
              status,severity,finding,missing_notes,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                call_id, str(itinerary), agent, center, caller, duration,
                "queued", "warning" if missing_notes else "info", finding,
                missing_notes, now, now,
            ),
        )
    print(f"[QA ALERT] Imported call {call_id} from {path.name}")
    return True


def scan_inbox_once():
    INBOX.mkdir(parents=True, exist_ok=True)
    imported = 0
    for path in sorted(INBOX.rglob("*.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0):
        if ingest_json_file(path):
            imported += 1
    return imported


def inbox_watcher():
    print(f"[QA ALERT] Watching inbox: {INBOX}")
    while True:
        try:
            scan_inbox_once()
        except Exception as exc:
            print(f"[QA ALERT] Inbox scan error: {exc}")
        time.sleep(5)


@app.on_event("startup")
def start_inbox_watcher():
    try:
        scan_inbox_once()
    except Exception as exc:
        print(f"[QA ALERT] Initial inbox scan error: {exc}")
    threading.Thread(target=inbox_watcher, daemon=True, name="qa-alert-inbox").start()


def slack_client():
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    if not token:
        raise HTTPException(400, "SLACK_BOT_TOKEN is not configured in .env")
    return WebClient(token=token)


def send_slack(text: str, recipient_id: str):
    rid = recipient_id.strip()
    if not rid:
        raise HTTPException(400, "Slack recipient ID is empty")
    client = slack_client()
    try:
        channel = rid
        if rid.startswith(("U", "W")):
            channel = client.conversations_open(users=[rid])["channel"]["id"]
        elif not rid.startswith(("C", "G", "D")):
            raise HTTPException(400, "Use a Slack member ID (U...) or channel/DM ID (C/G/D...)")
        r = client.chat_postMessage(channel=channel, text=text)
        return {"ok": True, "channel": r.get("channel"), "ts": r.get("ts")}
    except SlackApiError as e:
        raise HTTPException(400, f"Slack error: {e.response.get('error', 'unknown_error')}")


@app.get("/")
def home():
    if not FRONTEND.exists():
        raise HTTPException(404, "frontend/index.html missing")
    return FileResponse(FRONTEND)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "time": utc_now(),
        "inbox": str(INBOX),
        "inbox_exists": INBOX.exists(),
        "version": "0.4.0",
    }


@app.get("/api/auth/status")
def auth_status(request: Request):
    user = current_user_from_request(request)
    return {
        "authenticated": bool(user),
        "setup_required": user_count() == 0,
        "username": user["username"] if user else None,
    }


@app.post("/api/auth/setup")
def auth_setup(payload: AuthSetup):
    if user_count() > 0:
        raise HTTPException(409, "Admin account already exists")
    username = payload.username.strip()
    password = payload.password
    if len(username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    salt = secrets.token_hex(16)
    digest = password_digest(password, salt)
    with db() as c:
        c.execute(
            "INSERT INTO users(username,password_hash,salt,created_at) VALUES (?,?,?,?)",
            (username, digest, salt, utc_now()),
        )
    return {"ok": True}


@app.post("/api/auth/login")
def auth_login(payload: AuthLogin):
    username = payload.username.strip()
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        if not row:
            raise HTTPException(401, "Invalid username or password")
        digest = password_digest(payload.password, row["salt"])
        if not hmac.compare_digest(digest, row["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        token = secrets.token_urlsafe(48)
        ttl = REMEMBER_DAYS * 86400 if payload.remember else SESSION_HOURS * 3600
        expires = time.time() + ttl
        c.execute(
            "INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES (?,?,?,?)",
            (token, row["id"], expires, utc_now()),
        )
    response = JSONResponse({"ok": True, "username": row["username"]})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = request.cookies.get(COOKIE_NAME, "").strip()
    if token:
        with db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/api/settings")
def get_settings():
    s = read_settings()
    s["slack_token_configured"] = bool(os.getenv("SLACK_BOT_TOKEN", "").strip())
    return s


@app.put("/api/settings")
def put_settings(payload: Settings):
    with db() as c:
        for k, v in payload.model_dump().items():
            if isinstance(v, bool):
                v = "true" if v else "false"
            c.execute(
                "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, str(v)),
            )
    return read_settings()


@app.post("/api/slack/test")
def test_slack(payload: SlackTest):
    s = read_settings()
    rid = (payload.recipient_id or s["slack_recipient_id"]).strip()
    name = (payload.recipient_name or s["slack_recipient_name"]).strip()
    return send_slack(
        f"QA ALERT test message\nRecipient: {name or 'configured recipient'}\nStatus: Slack connection is working.",
        rid,
    )


@app.post("/api/inbox/rescan")
def rescan_inbox():
    return {"ok": True, "imported": scan_inbox_once(), "inbox": str(INBOX)}


@app.get("/api/cases/attention")
def attention_cases():
    with db() as c:
        rows = c.execute(
            """
            SELECT id,call_id,itinerary,agent,call_center,caller_number,duration_seconds,
                   status,severity,finding,missing_notes,created_at,updated_at
            FROM cases
            WHERE missing_notes=1 OR severity='critical' OR status='needs_attention'
            ORDER BY CASE WHEN severity='critical' THEN 0 ELSE 1 END, updated_at DESC
            """
        ).fetchall()
    return {"cases": [dict(r) for r in rows]}


@app.post("/api/cases/{case_id}/attention-resolution")
def resolve_attention(case_id: int, payload: AttentionResolution):
    action = payload.action.strip().lower()
    allowed = {"clear_missing_notes", "false_positive", "keep_attention"}
    if action not in allowed:
        raise HTTPException(400, "Unknown attention action")
    now = utc_now()
    with db() as c:
        row = c.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Case not found")
        c.execute(
            "INSERT INTO case_reviews(case_id,action,manager_note,correct_procedure,created_at) VALUES (?,?,?,?,?)",
            (case_id, action, payload.manager_note.strip(), payload.correct_procedure.strip(), now),
        )
        finding = row["finding"] or ""
        note = payload.manager_note.strip()
        if note:
            finding = (finding + "\nManager review: " + note).strip()
        if action == "clear_missing_notes":
            severity = "info" if row["severity"] == "warning" else row["severity"]
            c.execute(
                "UPDATE cases SET missing_notes=0,severity=?,finding=?,updated_at=? WHERE id=?",
                (severity, finding, now, case_id),
            )
        elif action == "false_positive":
            severity = "info" if row["severity"] == "warning" else row["severity"]
            c.execute(
                "UPDATE cases SET missing_notes=0,severity=?,status='reviewed',finding=?,updated_at=? WHERE id=?",
                (severity, finding, now, case_id),
            )
        else:
            c.execute(
                "UPDATE cases SET status='needs_attention',finding=?,updated_at=? WHERE id=?",
                (finding, now, case_id),
            )
        updated = c.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    return {"ok": True, "case": dict(updated)}


@app.get("/api/dashboard")
def dashboard():
    with db() as c:
        total = c.execute("SELECT COUNT(*) n FROM cases").fetchone()["n"]
        active = c.execute("SELECT COUNT(*) n FROM cases WHERE status IN ('queued','collecting','transcribing','qa_running')").fetchone()["n"]
        critical = c.execute("SELECT COUNT(*) n FROM cases WHERE severity='critical'").fetchone()["n"]
        missing = c.execute("SELECT COUNT(*) n FROM cases WHERE missing_notes=1").fetchone()["n"]
        attention = c.execute("SELECT COUNT(*) n FROM cases WHERE missing_notes=1 OR severity='critical' OR status='needs_attention'").fetchone()["n"]
    return {"total": total, "active": active, "critical": critical, "missing_notes": missing, "attention": attention}


@app.get("/api/export.xlsx")
def export_excel():
    with db() as c:
        rows = c.execute("SELECT * FROM cases ORDER BY created_at DESC").fetchall()
    wb = Workbook()
    ws = wb.active
    ws.title = "QA Cases"
    headers = [
        "Call ID", "Booking", "Agent", "Call Center", "Caller Number", "Duration Sec",
        "Guest Request", "Agent Action", "Score", "Status", "Severity", "Finding",
        "Missing Notes", "Evidence Start", "Evidence End", "Evidence Excerpt", "Evidence File",
        "Created At", "Updated At",
    ]
    ws.append(headers)
    for r in rows:
        ws.append([
            r["call_id"], r["itinerary"], r["agent"], r["call_center"], r["caller_number"],
            r["duration_seconds"], r["guest_request"], r["agent_action"], r["score"], r["status"],
            r["severity"], r["finding"], "Yes" if r["missing_notes"] else "No",
            r["evidence_start_sec"], r["evidence_end_sec"], r["evidence_excerpt"], r["evidence_path"],
            r["created_at"], r["updated_at"],
        ])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    out = EXPORTS / f"QA-ALERT-{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.xlsx"
    wb.save(out)
    return FileResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=out.name,
    )
