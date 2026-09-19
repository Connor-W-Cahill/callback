"""Background poller: watch the inbox and run new mail through the pipeline.

A thread, not a task queue. Serverless cannot run this at all, which is fine --
the email demo belongs to the local app, where the poller and the browser share
one process and one database.
"""
import threading
import time
import traceback

from src import config, db
from src.ingest import mailbox
from src.pipeline import process
from src.voice import telephony

POLL_SECONDS = 6

_state: dict = {
    "running": False,
    "address": None,
    "error": None,
    "last_poll": None,
    "processed": 0,
    "calls_placed": 0,
    "auto_call": False,
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

    _state.update(
        running=True,
        address=acct.address,
        error=None,
        auto_call=bool(config.AUTO_CALL and telephony.configured()),
    )

    while True:
        try:
            _poll_once(acct)
            _state["error"] = None
        except Exception as e:  # noqa: BLE001 - a bad poll must not end the loop
            _state["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        _state["last_poll"] = time.time()
        time.sleep(POLL_SECONDS)


def _maybe_auto_call(conn, decision) -> None:
    """Phase 4: an email that asks to move money triggers a real phone call.

    Gated twice over -- Twilio configured AND CALLBACK_AUTO_CALL=1 -- because
    this is the one thing in the project that reaches outside the laptop, and an
    inbound email is an untrusted trigger. The number still comes from the
    vendor master; nothing in the email chooses who we dial.
    """
    if not (config.AUTO_CALL and telephony.configured()):
        return
    if not decision.vendor:
        print("[mail] held, but no vendor on file to call")
        return
    try:
        from src.api.app import start_verification_call

        res = start_verification_call(conn, decision.hold_id)
        _state["calls_placed"] += 1
        print(f"[mail] auto-dialed {res['to']} for {decision.hold_id} (sid {res['sid']})")
    except Exception as e:  # noqa: BLE001 - a failed call must not stop the poller
        print(f"[mail] auto-call failed: {e}")
        _state["error"] = f"auto-call failed: {e}"


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
            decision = process(conn, message)
            _state["processed"] += 1
            print(f"[mail] processed {message['sender']}: {message['subject']!r} "
                  f"held={decision.held} score={decision.assessment.score}")

            if decision.held and decision.hold_id:
                _maybe_auto_call(conn, decision)
    finally:
        conn.close()
