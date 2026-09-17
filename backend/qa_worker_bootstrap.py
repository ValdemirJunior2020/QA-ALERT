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
from backend.knowledge_loader import load_knowledge_text

# Keep private Excel/Word Matrix files local and load them automatically.
qa_worker._knowledge_text = load_knowledge_text
qa_worker.main()
