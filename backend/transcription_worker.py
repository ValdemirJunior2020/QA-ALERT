from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = DATA / "qa-alert.db"
INBOX = Path.home() / "Downloads" / "QA-CALLS"
TRANSCRIPTS = DATA / "transcripts"
STATUS_FILE = DATA / "worker-status.json"
LOCK_FILE = DATA / "transcription-worker.lock"

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".webm", ".aac", ".flac"}
POLL_SECONDS = max(2, int(os.getenv("QA_TRANSCRIPTION_POLL_SECONDS", "3")))
MODEL_NAME = os.getenv("WHISPER_MODEL", "large-v3").strip() or "large-v3"
DEVICE = os.getenv("WHISPER_DEVICE", "cpu").strip() or "cpu"
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "float32").strip() or "float32"
LANGUAGE = os.getenv("WHISPER_LANGUAGE", "auto").strip().lower() or "auto"

HOTEL_PROMPT = (
    "Hotel reservation customer service call. The conversation may include HotelPlanner, "
    "hotel names, guest names, reservation numbers, itinerary numbers, confirmation numbers, "
    "check-in, check-out, room type, prepaid reservation, non-refundable reservation, "
    "cancellation, date change, modification, refund, refund queue, refund processing, "
    "free of charge waiver, FOC, voucher, supplier, RPP, claim link, customer service, "
    "front desk, booking details, and payment details. Transcribe exactly what is spoken."
)

DATA.mkdir(exist_ok=True)
TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
INBOX.mkdir(parents=True, exist_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def ensure_schema() -> None:
    columns = {
        "source_json_path": "TEXT",
        "audio_path": "TEXT",
        "transcript_text": "TEXT",
        "transcript_path": "TEXT",
        "transcript_json_path": "TEXT",
        "transcript_language": "TEXT",
        "transcription_started_at": "TEXT",
        "transcription_completed_at": "TEXT",
        "audio_deleted_at": "TEXT",
        "transcription_error": "TEXT",
    }
    with db() as c:
        existing = {r["name"] for r in c.execute("PRAGMA table_info(cases)").fetchall()}
        for name, sql_type in columns.items():
            if name not in existing:
                c.execute(f"ALTER TABLE cases ADD COLUMN {name} {sql_type}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cases_transcription_status ON cases(status)")


def write_status(**patch) -> None:
    current = {}
    try:
        if STATUS_FILE.exists():
            current = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        current = {}
    current.update(patch)
    current["updated_at"] = utc_now()
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS_FILE)


def acquire_single_instance_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except Exception:
        handle.close()
        return None


def get_call_id(data: dict, path: Path) -> str:
    value = data.get("call_id") or data.get("callId") or data.get("id")
    return str(value or f"FILE-{path.stem}").strip()


def resolve_audio_path(json_path: Path, data: dict, call_id: str) -> Path | None:
    raw = data.get("audio_file") or data.get("audio_path")
    candidates: list[Path] = []
    if raw:
        p = Path(str(raw))
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.extend([json_path.parent / p.name, INBOX / p, INBOX / p.name])
    for p in candidates:
        try:
            if p.exists() and p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
                return p.resolve()
        except Exception:
            pass

    # Fallback: collector filenames contain the Call ID.
    if call_id:
        try:
            for p in INBOX.rglob(f"*{call_id}*"):
                if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
                    return p.resolve()
        except Exception:
            pass
    return None


def reconcile_inbox() -> int:
    """Attach source JSON/audio paths to DB cases, including cases imported before this worker existed."""
    linked = 0
    try:
        json_files = sorted(INBOX.rglob("*.json"), key=lambda p: p.stat().st_mtime)
    except Exception:
        return 0

    with db() as c:
        for path in json_files:
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    continue
                call_id = get_call_id(data, path)
                audio = resolve_audio_path(path, data, call_id)
                row = c.execute("SELECT id,status FROM cases WHERE call_id=?", (call_id,)).fetchone()
                if not row:
                    continue
                c.execute(
                    "UPDATE cases SET source_json_path=?,audio_path=COALESCE(?,audio_path),updated_at=? WHERE call_id=?",
                    (str(path.resolve()), str(audio) if audio else None, utc_now(), call_id),
                )
                linked += 1
            except Exception as exc:
                print(f"[WHISPER] Could not reconcile {path.name}: {exc}")
    return linked


def find_audio_for_row(row: sqlite3.Row) -> Path | None:
    raw = row["audio_path"] if "audio_path" in row.keys() else None
    if raw:
        p = Path(raw)
        if p.exists() and p.is_file():
            return p
    call_id = row["call_id"]
    try:
        for p in INBOX.rglob(f"*{call_id}*"):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
                return p.resolve()
    except Exception:
        pass
    return None


def next_case() -> sqlite3.Row | None:
    with db() as c:
        rows = c.execute(
            """
            SELECT * FROM cases
            WHERE status IN ('queued','transcription_error')
              AND COALESCE(transcript_text,'')=''
            ORDER BY created_at ASC
            LIMIT 50
            """
        ).fetchall()
    for row in rows:
        audio = find_audio_for_row(row)
        if audio:
            with db() as c:
                now = utc_now()
                c.execute(
                    """
                    UPDATE cases
                    SET status='transcribing',audio_path=?,transcription_started_at=?,
                        transcription_error=NULL,updated_at=?
                    WHERE id=? AND status IN ('queued','transcription_error')
                    """,
                    (str(audio), now, now, row["id"]),
                )
                claimed = c.execute("SELECT * FROM cases WHERE id=?", (row["id"],)).fetchone()
            if claimed and claimed["status"] == "transcribing":
                return claimed
    return None


def load_model():
    from faster_whisper import WhisperModel

    threads = max(2, (os.cpu_count() or 4) - 1)
    kwargs = {
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
    }
    if DEVICE == "cpu":
        kwargs["cpu_threads"] = threads
    write_status(
        state="loading_model",
        model=MODEL_NAME,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        message=f"Loading Whisper {MODEL_NAME}...",
    )
    print(f"[WHISPER] Loading {MODEL_NAME} on {DEVICE} ({COMPUTE_TYPE})")
    model = WhisperModel(MODEL_NAME, **kwargs)
    write_status(
        state="idle",
        model=MODEL_NAME,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        message="Whisper ready",
    )
    print("[WHISPER] Model ready")
    return model


def collect_transcript(model, audio: Path) -> tuple[str, list[dict], list[dict], str, float | None]:
    lang = None if LANGUAGE == "auto" else LANGUAGE
    segments_iter, info = model.transcribe(
        str(audio),
        language=lang,
        task="transcribe",
        beam_size=10,
        best_of=10,
        patience=2.0,
        temperature=0.0,
        condition_on_previous_text=True,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 1000},
        initial_prompt=HOTEL_PROMPT,
    )
    segments: list[dict] = []
    words: list[dict] = []
    text_parts: list[str] = []
    for seg in segments_iter:
        text = str(seg.text or "").strip()
        if not text:
            continue
        start = round(float(seg.start), 3)
        end = round(float(seg.end), 3)
        segments.append({"start": start, "end": end, "text": text})
        text_parts.append(text)
        for word in (seg.words or []):
            if word.start is None or word.end is None:
                continue
            w = str(word.word or "").strip()
            if not w:
                continue
            words.append({
                "word": w,
                "start": round(float(word.start), 3),
                "end": round(float(word.end), 3),
                "probability": round(float(getattr(word, "probability", 0.0) or 0.0), 4),
            })
    language = str(getattr(info, "language", lang or "unknown") or "unknown")
    probability = getattr(info, "language_probability", None)
    return "\n".join(text_parts).strip(), segments, words, language, probability


def safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)
    return cleaned[:120] or "call"


def save_transcript(row: sqlite3.Row, text: str, segments: list[dict], words: list[dict], language: str, probability) -> tuple[Path, Path]:
    base = safe_name(row["call_id"])
    txt_path = TRANSCRIPTS / f"{base}.txt"
    json_path = TRANSCRIPTS / f"{base}.json"
    txt_tmp = txt_path.with_suffix(".txt.tmp")
    json_tmp = json_path.with_suffix(".json.tmp")
    txt_tmp.write_text(text, encoding="utf-8")
    json_tmp.write_text(
        json.dumps(
            {
                "call_id": row["call_id"],
                "agent": row["agent"],
                "call_center": row["call_center"],
                "language": language,
                "language_probability": probability,
                "model": MODEL_NAME,
                "device": DEVICE,
                "compute_type": COMPUTE_TYPE,
                "created_at": utc_now(),
                "segments": segments,
                "words": words,
                "text": text,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    txt_tmp.replace(txt_path)
    json_tmp.replace(json_path)
    return txt_path, json_path


def delete_full_audio(row: sqlite3.Row, primary: Path) -> tuple[bool, list[str], list[str]]:
    targets: set[Path] = {primary}
    call_id = row["call_id"]
    try:
        for p in INBOX.rglob(f"*{call_id}*"):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
                targets.add(p)
    except Exception:
        pass

    deleted: list[str] = []
    errors: list[str] = []
    for p in targets:
        try:
            if p.exists():
                p.unlink()
                deleted.append(str(p))
        except Exception as exc:
            errors.append(f"{p}: {exc}")
    return not errors, deleted, errors


def process_case(model, row: sqlite3.Row) -> None:
    audio = find_audio_for_row(row)
    if not audio:
        with db() as c:
            c.execute(
                "UPDATE cases SET status='queued',transcription_error=?,updated_at=? WHERE id=?",
                ("Audio file was not found yet.", utc_now(), row["id"]),
            )
        return

    write_status(
        state="transcribing",
        current_call_id=row["call_id"],
        current_agent=row["agent"],
        audio_path=str(audio),
        started_at=utc_now(),
        message=f"Transcribing {row['call_id']}",
    )
    print(f"[WHISPER] Transcribing {row['call_id']} - {row['agent']}")

    try:
        text, segments, words, language, probability = collect_transcript(model, audio)
        if not text:
            raise RuntimeError("Whisper returned an empty transcript; audio was kept for retry/review.")

        txt_path, json_path = save_transcript(row, text, segments, words, language, probability)
        completed = utc_now()
        with db() as c:
            c.execute(
                """
                UPDATE cases
                SET transcript_text=?,transcript_path=?,transcript_json_path=?,transcript_language=?,
                    transcription_completed_at=?,status='transcribed',transcription_error=NULL,updated_at=?
                WHERE id=?
                """,
                (text, str(txt_path), str(json_path), language, completed, completed, row["id"]),
            )

        cleanup_ok, deleted, cleanup_errors = delete_full_audio(row, audio)
        cleanup_time = utc_now()
        with db() as c:
            if cleanup_ok:
                c.execute(
                    """
                    UPDATE cases
                    SET status='transcribed_audio_deleted',audio_deleted_at=?,audio_path=NULL,updated_at=?
                    WHERE id=?
                    """,
                    (cleanup_time, cleanup_time, row["id"]),
                )
            else:
                c.execute(
                    """
                    UPDATE cases
                    SET status='transcribed_audio_cleanup_error',transcription_error=?,updated_at=?
                    WHERE id=?
                    """,
                    (" | ".join(cleanup_errors), cleanup_time, row["id"]),
                )

        if cleanup_ok:
            print(f"[WHISPER] Completed {row['call_id']} - transcript saved; full audio deleted")
            write_status(
                state="idle",
                last_completed_call_id=row["call_id"],
                last_completed_at=cleanup_time,
                last_audio_deleted=True,
                last_deleted_files=deleted,
                current_call_id=None,
                current_agent=None,
                audio_path=None,
                message="Whisper ready - last call completed and audio deleted",
            )
        else:
            print(f"[WHISPER] Completed transcript for {row['call_id']} but audio cleanup had errors")
            write_status(
                state="cleanup_error",
                last_completed_call_id=row["call_id"],
                last_completed_at=cleanup_time,
                last_audio_deleted=False,
                last_error=" | ".join(cleanup_errors),
                current_call_id=None,
                current_agent=None,
                message="Transcript saved, but full audio could not be fully deleted",
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        now = utc_now()
        print(f"[WHISPER] ERROR {row['call_id']}: {error}")
        with db() as c:
            c.execute(
                "UPDATE cases SET status='transcription_error',transcription_error=?,updated_at=? WHERE id=?",
                (error, now, row["id"]),
            )
        write_status(
            state="error",
            current_call_id=None,
            current_agent=None,
            last_error=error,
            last_error_call_id=row["call_id"],
            message="Transcription error - full audio was kept",
        )
        time.sleep(5)


def run_forever() -> None:
    lock_handle = acquire_single_instance_lock()
    if lock_handle is None:
        print("[WHISPER] Another transcription worker is already running. Exiting duplicate worker.")
        return

    ensure_schema()
    write_status(
        state="starting",
        pid=os.getpid(),
        model=MODEL_NAME,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        inbox=str(INBOX),
        message="Starting Whisper worker",
    )
    print(f"[WHISPER] Watching {INBOX}")

    model = None
    while True:
        try:
            reconcile_inbox()
            row = next_case()
            if not row:
                if model is None:
                    write_status(
                        state="idle_waiting",
                        pid=os.getpid(),
                        model=MODEL_NAME,
                        device=DEVICE,
                        compute_type=COMPUTE_TYPE,
                        message="Waiting for an audio call",
                    )
                else:
                    write_status(
                        state="idle",
                        pid=os.getpid(),
                        model=MODEL_NAME,
                        device=DEVICE,
                        compute_type=COMPUTE_TYPE,
                        message="Whisper ready - waiting for next call",
                    )
                time.sleep(POLL_SECONDS)
                continue

            if model is None:
                try:
                    model = load_model()
                except Exception as exc:
                    error = f"Could not load Whisper {MODEL_NAME}: {type(exc).__name__}: {exc}"
                    print(f"[WHISPER] {error}")
                    with db() as c:
                        c.execute(
                            "UPDATE cases SET status='queued',transcription_error=?,updated_at=? WHERE id=?",
                            (error, utc_now(), row["id"]),
                        )
                    write_status(state="model_error", last_error=error, message=error)
                    time.sleep(30)
                    continue

            process_case(model, row)
        except KeyboardInterrupt:
            write_status(state="stopped", message="Worker stopped")
            break
        except Exception as exc:
            error = f"Worker loop error: {type(exc).__name__}: {exc}"
            print(f"[WHISPER] {error}")
            write_status(state="error", last_error=error, message=error)
            time.sleep(5)

    # Keep the lock file handle alive until process exit.
    _ = lock_handle


if __name__ == "__main__":
    run_forever()
