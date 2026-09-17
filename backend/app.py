from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
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
DATA.mkdir(exist_ok=True)
EXPORTS.mkdir(exist_ok=True)
INBOX.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="QA ALERT", version="0.2.0")

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
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
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
        """)
        for k,v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)",(k,v))

init_db()

class Settings(BaseModel):
    scan_interval_minutes:int=5
    minimum_call_seconds:int=60
    max_active_calls:int=5
    transcription_workers:int=1
    ollama_workers:int=2
    slack_enabled:bool=False
    slack_recipient_name:str="Valdemir Gonçalves Junior"
    slack_recipient_id:str=""
    evidence_padding_before_seconds:int=8
    evidence_padding_after_seconds:int=8

class SlackTest(BaseModel):
    recipient_id:Optional[str]=None
    recipient_name:Optional[str]=None


def read_settings():
    with db() as c:
        raw={r['key']:r['value'] for r in c.execute("SELECT key,value FROM settings")}
    return {
      "scan_interval_minutes":int(raw.get("scan_interval_minutes",5)),
      "minimum_call_seconds":int(raw.get("minimum_call_seconds",60)),
      "max_active_calls":int(raw.get("max_active_calls",5)),
      "transcription_workers":int(raw.get("transcription_workers",1)),
      "ollama_workers":int(raw.get("ollama_workers",2)),
      "slack_enabled":raw.get("slack_enabled","false")=="true",
      "slack_recipient_name":raw.get("slack_recipient_name",""),
      "slack_recipient_id":raw.get("slack_recipient_id",""),
      "evidence_padding_before_seconds":int(raw.get("evidence_padding_before_seconds",8)),
      "evidence_padding_after_seconds":int(raw.get("evidence_padding_after_seconds",8)),
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
        # Do not silently drop a call just because one metadata field is missing.
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
    if isinstance(docs, list):
        docs = "\n".join(str(x) for x in docs if x)
    missing_notes = 0 if (docs and str(docs).strip()) else 1

    now = datetime.now(timezone.utc).isoformat()
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
    # Collector versions have saved both directly in QA-CALLS and in subfolders,
    # so scan recursively and dedupe by call_id in SQLite.
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
    # Fast first pass so a call that already exists before START.bat launches
    # appears in the dashboard immediately.
    try:
        scan_inbox_once()
    except Exception as exc:
        print(f"[QA ALERT] Initial inbox scan error: {exc}")
    threading.Thread(target=inbox_watcher, daemon=True, name="qa-alert-inbox").start()


def slack_client():
    token=os.getenv("SLACK_BOT_TOKEN","").strip()
    if not token:
        raise HTTPException(400,"SLACK_BOT_TOKEN is not configured in .env")
    return WebClient(token=token)


def send_slack(text:str, recipient_id:str):
    rid=recipient_id.strip()
    if not rid:
        raise HTTPException(400,"Slack recipient ID is empty")
    client=slack_client()
    try:
        channel=rid
        if rid.startswith(("U","W")):
            channel=client.conversations_open(users=[rid])["channel"]["id"]
        elif not rid.startswith(("C","G","D")):
            raise HTTPException(400,"Use a Slack member ID (U...) or channel/DM ID (C/G/D...)")
        r=client.chat_postMessage(channel=channel,text=text)
        return {"ok":True,"channel":r.get("channel"),"ts":r.get("ts")}
    except SlackApiError as e:
        raise HTTPException(400,f"Slack error: {e.response.get('error','unknown_error')}")

@app.get("/")
def home():
    if not FRONTEND.exists(): raise HTTPException(404,"frontend/index.html missing")
    return FileResponse(FRONTEND)

@app.get("/api/health")
def health():
    return {
        "ok":True,
        "time":datetime.now(timezone.utc).isoformat(),
        "inbox":str(INBOX),
        "inbox_exists":INBOX.exists(),
    }

@app.get("/api/settings")
def get_settings():
    s=read_settings(); s["slack_token_configured"]=bool(os.getenv("SLACK_BOT_TOKEN","").strip()); return s

@app.put("/api/settings")
def put_settings(payload:Settings):
    with db() as c:
        for k,v in payload.model_dump().items():
            if isinstance(v,bool): v="true" if v else "false"
            c.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,str(v)))
    return read_settings()

@app.post("/api/slack/test")
def test_slack(payload:SlackTest):
    s=read_settings(); rid=(payload.recipient_id or s["slack_recipient_id"]).strip(); name=(payload.recipient_name or s["slack_recipient_name"]).strip()
    return send_slack(f"QA ALERT test message\nRecipient: {name or 'configured recipient'}\nStatus: Slack connection is working.",rid)

@app.post("/api/inbox/rescan")
def rescan_inbox():
    return {"ok": True, "imported": scan_inbox_once(), "inbox": str(INBOX)}

@app.get("/api/dashboard")
def dashboard():
    with db() as c:
        total=c.execute("SELECT COUNT(*) n FROM cases").fetchone()["n"]
        active=c.execute("SELECT COUNT(*) n FROM cases WHERE status IN ('queued','collecting','transcribing','qa_running')").fetchone()["n"]
        critical=c.execute("SELECT COUNT(*) n FROM cases WHERE severity='critical'").fetchone()["n"]
        missing=c.execute("SELECT COUNT(*) n FROM cases WHERE missing_notes=1").fetchone()["n"]
    return {"total":total,"active":active,"critical":critical,"missing_notes":missing}

@app.get("/api/export.xlsx")
def export_excel():
    with db() as c: rows=c.execute("SELECT * FROM cases ORDER BY created_at DESC").fetchall()
    wb=Workbook(); ws=wb.active; ws.title="QA Cases"
    headers=["Call ID","Booking","Agent","Call Center","Caller Number","Duration Sec","Guest Request","Agent Action","Score","Status","Severity","Finding","Missing Notes","Evidence Start","Evidence End","Evidence Excerpt","Evidence File","Created At","Updated At"]
    ws.append(headers)
    for r in rows:
        ws.append([r["call_id"],r["itinerary"],r["agent"],r["call_center"],r["caller_number"],r["duration_seconds"],r["guest_request"],r["agent_action"],r["score"],r["status"],r["severity"],r["finding"],"Yes" if r["missing_notes"] else "No",r["evidence_start_sec"],r["evidence_end_sec"],r["evidence_excerpt"],r["evidence_path"],r["created_at"],r["updated_at"]])
    ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions
    out=EXPORTS/f"QA-ALERT-{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.xlsx"; wb.save(out)
    return FileResponse(out,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",filename=out.name)
