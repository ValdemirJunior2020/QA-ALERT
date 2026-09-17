from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "qa-alert.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    if not DB.exists():
        return
    with sqlite3.connect(DB, timeout=20) as c:
        c.row_factory = sqlite3.Row
        cols = {r[1] for r in c.execute("PRAGMA table_info(cases)").fetchall()}
        if "transcript_text" not in cols or "qa_result_json" not in cols:
            return

        # Legacy rows may have been marked complete before the QA worker existed.
        # If they already have a transcript but no QA result, put them back into
        # the post-transcription queue so the new QA worker evaluates them.
        cur = c.execute(
            """
            UPDATE cases
            SET status='transcribed_audio_deleted', updated_at=?
            WHERE COALESCE(transcript_text,'')<>''
              AND COALESCE(qa_result_json,'')=''
              AND status IN ('complete','completed','completed_no_booking','reviewed')
            """,
            (utc_now(),),
        )
        repaired = cur.rowcount or 0
    if repaired:
        print(f"[QA ALERT] Re-queued {repaired} legacy transcribed case(s) for QA.")


if __name__ == "__main__":
    main()
