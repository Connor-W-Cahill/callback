"""Record every external service call so the UI can show what actually happened.

Two tracks reward this directly: Nemotron asks us to show where the model fits
and prove it works, ElevenLabs asks that voice be a real part of how the project
works. A judge should not have to take our word for either -- they can watch the
calls land.
"""
import sqlite3
import threading
from datetime import datetime, timezone

from src import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS service_call (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    service TEXT NOT NULL,      -- nemotron | elevenlabs
    job TEXT NOT NULL,          -- extract | score | judge | tts | stt
    model TEXT,                 -- model id or voice id
    ok INTEGER NOT NULL,
    ms INTEGER NOT NULL,
    units INTEGER,              -- characters for elevenlabs, tokens-ish for nemotron
    detail TEXT,                -- error text, or a note
    prompt_excerpt TEXT,
    response_excerpt TEXT,
    req_id TEXT          -- attempts of ONE logical call share this
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(service_call)")}
    if "req_id" not in cols:
        c.execute("ALTER TABLE service_call ADD COLUMN req_id TEXT")
    return c


def record(
    *,
    service: str,
    job: str,
    model: str | None,
    ok: bool,
    ms: int,
    units: int | None = None,
    detail: str = "",
    prompt_excerpt: str = "",
    response_excerpt: str = "",
    req_id: str = "",
) -> None:
    """Best effort: telemetry must never break the pipeline it is watching."""
    try:
        with _lock:
            c = _conn()
            c.execute(
                """INSERT INTO service_call
                   (at, service, job, model, ok, ms, units, detail, prompt_excerpt,
                    response_excerpt, req_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    service,
                    job,
                    model,
                    1 if ok else 0,
                    ms,
                    units,
                    detail[:400],
                    prompt_excerpt[:1200],
                    response_excerpt[:1200],
                    req_id,
                ),
            )
            c.commit()
            c.close()
    except Exception:  # noqa: BLE001 - never let logging break the run
        pass


def recent(limit: int = 120) -> list[dict]:
    try:
        c = _conn()
        rows = c.execute(
            "SELECT * FROM service_call ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        c.close()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def clear() -> None:
    try:
        with _lock:
            c = _conn()
            c.execute("DELETE FROM service_call")
            c.commit()
            c.close()
    except Exception:  # noqa: BLE001
        pass
