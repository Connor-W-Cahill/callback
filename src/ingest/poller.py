"""Background poller: watch the inbox and run new mail through the pipeline.

A thread, not a task queue. Serverless cannot run this at all, which is fine --
the email demo belongs to the local app, where the poller and the browser share
one process and one database.
"""
import threading
import time
import traceback

from src import db
from src.ingest import mailbox
from src.pipeline import process

POLL_SECONDS = 6

_state: dict = {
    "running": False,
    "address": None,
    "error": None,
    "last_poll": None,
    "processed": 0,
}
_thread: threading.Thread | None = None


def state() -> dict:
    return dict(_state)


def start() -> None:
    """Start polling once per process."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="mail-poller", daemon=True)
    _thread.start()


def _loop() -> None:
    try:
        acct = mailbox.load_or_create()
    except Exception as e:  # noqa: BLE001 - surface it in the UI, do not crash the app
        _state["error"] = f"could not provision an inbox: {e}"
        return

    _state.update(running=True, address=acct.address, error=None)

    while True:
        try:
            _poll_once(acct)
            _state["error"] = None
        except Exception as e:  # noqa: BLE001 - a bad poll must not end the loop
            _state["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        _state["last_poll"] = time.time()
        time.sleep(POLL_SECONDS)


def _poll_once(acct: mailbox.Account) -> None:
    conn = db.connect()
    db.init(conn)
    try:
        for summary in mailbox.list_messages(acct):
            msg_id = f"mail-{summary['id']}"
            seen = conn.execute("SELECT 1 FROM message WHERE id=?", (msg_id,)).fetchone()
            if seen:
                continue
            full = mailbox.fetch(acct, summary["id"])
            message = mailbox.to_message(full)
            process(conn, message)
            _state["processed"] += 1
            print(f"[mail] processed {message['sender']}: {message['subject']!r}")
    finally:
        conn.close()
