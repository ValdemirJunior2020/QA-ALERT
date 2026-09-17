from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "qa-alert.db"


def database_ready() -> bool:
    try:
        if not DB.exists():
            return False
        with sqlite3.connect(DB, timeout=5) as c:
            row = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cases'").fetchone()
            return bool(row)
    except Exception:
        return False


while not database_ready():
    print("[QA WORKER] Waiting for QA ALERT database...")
    time.sleep(2)

import backend.qa_worker as qa_worker
from backend.booking_rules import read_automation_settings
from backend.knowledge_loader import load_knowledge_text

# Keep private Excel/Word Matrix files local and load them automatically.
qa_worker._knowledge_text = load_knowledge_text

# Respect the self-service setting when multiple bookings cannot be matched to one.
_original_save_match = qa_worker._save_match

def _save_match_with_setting(case_row, chosen, confidence, status):
    if chosen is not None:
        return _original_save_match(case_row, chosen, confidence, status)
    settings = read_automation_settings()
    action = settings.get("multi_booking_no_match_action", "needs_attention")
    now = qa_worker.utc_now()
    with qa_worker.db() as c:
        c.execute("UPDATE booking_candidates SET is_matched=0,match_confidence=NULL WHERE case_id=?", (case_row["id"],))
        new_status = "needs_attention" if action == "needs_attention" else "booking_match_hold"
        finding = "Multiple bookings were found, but one unique booking could not be matched to the call. QA was not run on multiple bookings."
        c.execute(
            "UPDATE cases SET booking_match_status=?,booking_match_confidence=?,status=?,finding=?,updated_at=? WHERE id=?",
            (status, confidence, new_status, finding, now, case_row["id"]),
        )

qa_worker._save_match = _save_match_with_setting
qa_worker.main()
