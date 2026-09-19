"""SQLite storage. stdlib sqlite3, no ORM -- the schema is six tables."""
import json
import sqlite3
from pathlib import Path

from src import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendor (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domains TEXT NOT NULL,
    known_senders TEXT NOT NULL,
    phone_on_file TEXT NOT NULL,
    contact_name TEXT,
    account TEXT NOT NULL,
    routing TEXT NOT NULL,
    country TEXT NOT NULL,
    bank_name TEXT,
    category TEXT
);

CREATE TABLE IF NOT EXISTS payment (
    invoice_number TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES vendor(id),
    amount REAL NOT NULL,
    paid_to_account TEXT NOT NULL,
    paid_to_routing TEXT NOT NULL,
    paid_on TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message (
    id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    sender TEXT NOT NULL,
    reply_to TEXT,
    subject TEXT,
    body TEXT NOT NULL,
    attachment_text TEXT,
    extracted TEXT,
    status TEXT NOT NULL DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS hold (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES message(id),
    vendor_id TEXT,
    score REAL NOT NULL,
    rationale TEXT NOT NULL,
    signals TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification (
    id TEXT PRIMARY KEY,
    hold_id TEXT NOT NULL REFERENCES hold(id),
    dialed_number TEXT NOT NULL,
    transcript TEXT,
    audio_path TEXT,
    judgment TEXT,
    judge_quote TEXT,
    judge_reasoning TEXT,
    reply_audio_path TEXT,
    reply_source TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    subject TEXT,
    detail TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to databases created by an earlier version."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(verification)")}
    for col in ("reply_audio_path", "reply_source"):
        if col not in have:
            conn.execute(f"ALTER TABLE verification ADD COLUMN {col} TEXT")


def seed(conn: sqlite3.Connection) -> None:
    """Load the synthetic vendor master and payment history. Idempotent."""
    vendors = json.loads((config.SEED / "vendors.json").read_text())
    for v in vendors:
        conn.execute(
            """INSERT OR REPLACE INTO vendor
               (id, name, domains, known_senders, phone_on_file, contact_name,
                account, routing, country, bank_name, category)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                v["id"],
                v["name"],
                json.dumps(v["domains"]),
                json.dumps(v["known_senders"]),
                v["phone_on_file"],
                v.get("contact_name"),
                v["bank"]["account"],
                v["bank"]["routing"],
                v["bank"]["country"],
                v["bank"].get("bank_name"),
                v.get("category"),
            ),
        )

    history_file = config.SEED / "payment_history.json"
    if history_file.exists():
        for p in json.loads(history_file.read_text()):
            conn.execute(
                """INSERT OR REPLACE INTO payment
                   (invoice_number, vendor_id, amount, paid_to_account, paid_to_routing, paid_on)
                   VALUES (?,?,?,?,?,?)""",
                (
                    p["invoice_number"],
                    p["vendor_id"],
                    p["amount"],
                    p["paid_to_account"],
                    p["paid_to_routing"],
                    p["paid_on"],
                ),
            )
    conn.commit()


def clear(conn: sqlite3.Connection) -> None:
    """Empty every table, in FK-safe order. Used by /api/reset between demo runs."""
    for table in ("verification", "hold", "message", "audit", "payment", "vendor"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def vendors(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM vendor").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["domains"] = json.loads(d["domains"])
        d["known_senders"] = json.loads(d["known_senders"])
        out.append(d)
    return out


def payments_for(conn: sqlite3.Connection, vendor_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM payment WHERE vendor_id = ? ORDER BY paid_on DESC LIMIT ?",
        (vendor_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def log(conn: sqlite3.Connection, actor: str, action: str, subject: str = "", detail: str = "") -> None:
    from datetime import datetime, timezone

    conn.execute(
        "INSERT INTO audit (at, actor, action, subject, detail) VALUES (?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), actor, action, subject, detail),
    )
    conn.commit()
