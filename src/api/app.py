"""Backend for the AP clerk's hold queue."""
import base64
import secrets
from contextlib import contextmanager
import json
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles

from src import config, db, llm, telemetry
from src.extract import extractor
from src.ingest import inbox, poller
from src.pipeline import VerificationConflict, process, verify
from src.voice import agent as voice
from src.voice import telephony

basic_auth = HTTPBasic(auto_error=False)


async def require_access(request: Request):
    # Provider callbacks authenticate with their signature, not browser credentials.
    if request.url.path in ("/api/twilio/recording", "/api/twilio/status"):
        return
    if not config.API_PASSWORD:
        if not config.DEMO_MODE:
            raise HTTPException(503, "Live mode requires CALLBACK_API_PASSWORD")
        return
    credentials = await basic_auth(request)
    if not credentials or not secrets.compare_digest(credentials.password.encode(), config.API_PASSWORD.encode()):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": 'Basic realm="Callback"'})


def require_demo():
    if not config.DEMO_MODE:
        raise HTTPException(403, "This operation is available only in demo mode")


app = FastAPI(title="Callback", description="Vendor payment-change fraud interceptor",
              dependencies=[Depends(require_access)])
WEB = config.ROOT / "web"


_seed_lock = threading.Lock()


@contextmanager
def conn(*, ensure_seeded: bool = False):
    c = db.connect()
    try:
        db.init(c)
        if ensure_seeded:
            _ensure_vendors(c)
        yield c
    finally:
        c.close()


def _ensure_vendors(c) -> None:
    """Make sure the vendor master exists. Nothing else.

    Vendors are reference data -- inbound mail cannot be resolved without them,
    and on serverless each instance has its own empty /tmp. The demo *inbox* is
    deliberately NOT loaded here: a page reload should not conjure payment
    changes that nobody sent. Those arrive by email, or from the Reset button.
    """
    if c.execute("SELECT COUNT(*) FROM vendor").fetchone()[0]:
        return
    with _seed_lock:
        if c.execute("SELECT COUNT(*) FROM vendor").fetchone()[0]:
            return
        db.seed(c)


@app.on_event("startup")
def _start_poller() -> None:
    # Serverless has no long-lived process to poll from; the email demo is local.
    if not config.SERVERLESS and os.getenv("CALLBACK_EMAIL", "1") != "0":
        poller.start()


@app.get("/api/mailbox")
def mailbox_status():
    """The address to email, and whether the poller is alive."""
    st = poller.state()
    with conn() as c:
        st["received"] = c.execute(
            "SELECT COUNT(*) FROM message WHERE id LIKE 'mail-%'"
        ).fetchone()[0]
        st["poll_seconds"] = poller.POLL_SECONDS
        return st


# --- telephony (phase 4) -------------------------------------------------

def start_verification_call(c, hold_id: str) -> dict:
    """Dial the vendor for real and let the recording come back by webhook."""
    if config.DEMO_MODE or not config.API_PASSWORD:
        raise HTTPException(403, "Real dialing requires live mode and CALLBACK_API_PASSWORD")
    if not telephony.configured():
        raise HTTPException(400, "Twilio is not configured (see README: phase 4)")

    hold = c.execute("SELECT * FROM hold WHERE id=?", (hold_id,)).fetchone()
    if hold is None:
        raise HTTPException(404, "no such hold")
    if not hold["vendor_id"]:
        raise HTTPException(400, "cannot verify: no vendor on file to call")
    vendor = dict(c.execute("SELECT * FROM vendor WHERE id=?", (hold["vendor_id"],)).fetchone())
    msg = c.execute("SELECT extracted FROM message WHERE id=?", (hold["message_id"],)).fetchone()
    ex = extractor.Extraction(**json.loads(msg["extracted"]))

    call_token = uuid.uuid4().hex
    with c:
        changed = c.execute(
            "UPDATE hold SET status='calling', call_sid=NULL, call_token=? WHERE id=? AND status IN ('held','escalated')",
            (call_token, hold_id),
        ).rowcount
        if not changed:
            raise HTTPException(409, "hold is settled or already being verified")
    try:
        line = voice.script_for(vendor, ex)
        # Provider TTS avoids an authenticated audio URL and shared-file races.
        action = f"{config.PUBLIC_BASE_URL}/api/twilio/recording?hold_id={hold_id}&call_token={call_token}"
        handle = telephony.place_call(
            to=vendor["phone_on_file"],
            twiml=telephony.twiml_for(None, line, action),
            status_callback=f"{config.PUBLIC_BASE_URL}/api/twilio/status?hold_id={hold_id}&call_token={call_token}",
        )
        with c:
            c.execute("UPDATE hold SET call_sid=? WHERE id=? AND call_token=? AND call_sid IS NULL",
                      (handle.sid, hold_id, call_token))
            db.log(c, "twilio", "call_placed", hold_id,
                   f"dialed {vendor['phone_on_file']} sid={handle.sid}", commit=False)
        return {"sid": handle.sid, "to": handle.to, "status": handle.status, "hold_status": "calling"}
    except Exception:
        with c:
            c.execute("UPDATE hold SET status='escalated' WHERE id=? AND status='calling' AND call_sid IS NULL", (hold_id,))
        raise


@app.post("/api/holds/{hold_id}/dial")
def dial(hold_id: str):
    """Place the verification call over the real phone network."""
    with conn(ensure_seeded=True) as c:
        return start_verification_call(c, hold_id)


@app.post("/api/twilio/recording")
async def twilio_recording(request: Request, hold_id: str = "", call_token: str = ""):
    """Twilio posts the vendor's recorded answer here. Transcribe, judge, resolve."""
    form = dict(await request.form())
    url = str(request.url)
    if not telephony.valid_signature(url, form, request.headers.get("X-Twilio-Signature", "")):
        # This endpoint acts on what it receives, so an unsigned caller could
        # drive the fraud control. Refuse anything that does not verify.
        raise HTTPException(403, "bad Twilio signature")

    return await run_in_threadpool(_recording_received, form, hold_id, call_token)


def _bind_call(c, hold_id: str, call_token: str, call_sid: str):
    if not call_token or not call_sid:
        raise HTTPException(409, "callback is missing its call identity")
    with c:
        c.execute("UPDATE hold SET call_sid=? WHERE id=? AND call_token=? AND call_sid IS NULL",
                  (call_sid, hold_id, call_token))
        hold = c.execute("SELECT call_sid, call_token FROM hold WHERE id=?", (hold_id,)).fetchone()
        if not hold or hold["call_token"] != call_token or hold["call_sid"] != call_sid:
            raise HTTPException(409, "callback does not belong to the active call")


def _recording_received(form: dict, hold_id: str, call_token: str = ""):
    rec_url = form.get("RecordingUrl")
    rec_sid = form.get("RecordingSid", "")
    if not rec_url or not rec_sid or not form.get("CallSid"):
        return Response(content="<Response/>", media_type="application/xml")

    with conn() as c:
        _bind_call(c, hold_id, call_token, form["CallSid"])
        # Twilio posts the same recording to BOTH the Record action and the
        # recordingStatusCallback, so without this the clip is transcribed and
        # judged twice -- double the STT spend for an identical verdict.
        if rec_sid and c.execute(
            "SELECT 1 FROM recording_receipt WHERE recording_sid=?", (rec_sid,)
        ).fetchone():
            return Response(content="<Response/>", media_type="application/xml")

        try:
            result = verify(
                c, hold_id,
                reply_audio=telephony.fetch_recording(rec_url),
                audio_filename="twilio.wav", recording_sid=rec_sid, call_sid=form["CallSid"],
            )
            db.log(c, "twilio", f"call_{result['judgment']}", hold_id, result["reasoning"])
        except VerificationConflict:
            return Response(content="<Response/>", media_type="application/xml")
        except Exception as e:  # noqa: BLE001 - retain failure evidence
            db.log(c, "twilio", "call_failed", hold_id, str(e)[:200])
            raise HTTPException(503, "Recording processing failed; retry required") from e

        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response>'
                    '<Say voice="Polly.Joanna">Thank you. Goodbye.</Say></Response>',
            media_type="application/xml",
        )


@app.post("/api/twilio/status")
async def twilio_status(request: Request, hold_id: str = "", call_token: str = ""):
    """Call lifecycle events: no answer, busy, failed."""
    form = dict(await request.form())
    if not telephony.valid_signature(str(request.url), form, request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(403, "bad Twilio signature")
    return await run_in_threadpool(_call_status_received, form, hold_id, call_token)


def _call_status_received(form: dict, hold_id: str, call_token: str = ""):
    status = form.get("CallStatus", "")
    if status in ("no-answer", "busy", "failed", "canceled", "completed"):
        with conn() as c:
            _bind_call(c, hold_id, call_token, form.get("CallSid", ""))
            # A call that ended without a recording -- hung up early, voicemail, or
            # silence past the record timeout -- still has to resolve, or the hold
            # sits on 'calling' forever and the clerk never learns anything.
            changed = c.execute(
                "UPDATE hold SET status='escalated' WHERE id=? AND status='calling' AND call_sid=?",
                (hold_id, form.get("CallSid", "")),
            ).rowcount
            c.commit()
            if changed:
                db.log(c, "twilio", f"call_{status}", hold_id,
                       f"call ended ({status}) without a processed recording; payment stays held")
    return Response(content="<Response/>", media_type="application/xml")


# --- vendor master -------------------------------------------------------

def _clean_list(v) -> list[str]:
    """Accept a list or a comma/newline separated string from the form."""
    if isinstance(v, list):
        items = v
    else:
        items = re.split(r"[,\n]", str(v or ""))
    return [x.strip().lower() for x in items if str(x).strip()]


@app.get("/api/vendors")
def list_vendors():
    with conn(ensure_seeded=True) as c:
        out = []
        for v in db.vendors(c):
            v["payments"] = c.execute(
                "SELECT COUNT(*) FROM payment WHERE vendor_id=?", (v["id"],)
            ).fetchone()[0]
            out.append(v)
        return sorted(out, key=lambda v: v["name"].lower())


@app.put("/api/vendors/{vendor_id}", dependencies=[Depends(require_demo)])
def upsert_vendor(vendor_id: str, body: dict):
    """Create or update a vendor. Always marks it custom so /api/reset keeps it."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    account = re.sub(r"\D", "", str(body.get("account") or ""))
    if not account:
        raise HTTPException(400, "account number is required -- it is the signal everything hangs on")

    with conn(ensure_seeded=True) as c:
        c.execute(
            """INSERT OR REPLACE INTO vendor
               (id, name, domains, known_senders, phone_on_file, contact_name,
                account, routing, country, bank_name, category, custom)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
            (
                vendor_id,
                name,
                json.dumps(_clean_list(body.get("domains"))),
                json.dumps(_clean_list(body.get("known_senders"))),
                (body.get("phone_on_file") or "").strip(),
                (body.get("contact_name") or "").strip() or None,
                account,
                re.sub(r"\D", "", str(body.get("routing") or "")),
                (body.get("country") or "US").strip().upper(),
                (body.get("bank_name") or "").strip() or None,
                (body.get("category") or "").strip() or None,
            ),
        )
        c.commit()
        db.log(c, "user", "vendor_saved", vendor_id, name)
        return {"ok": True, "id": vendor_id}


@app.post("/api/vendors", dependencies=[Depends(require_demo)])
def create_vendor(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "vendor"
    with conn(ensure_seeded=True) as c:
        vid, n = f"v-{slug}", 1
        while c.execute("SELECT 1 FROM vendor WHERE id=?", (vid,)).fetchone():
            n += 1
            vid = f"v-{slug}-{n}"
        return upsert_vendor(vid, body)


@app.delete("/api/vendors/{vendor_id}", dependencies=[Depends(require_demo)])
def delete_vendor(vendor_id: str):
    with conn(ensure_seeded=True) as c:
        if not c.execute("SELECT 1 FROM vendor WHERE id=?", (vendor_id,)).fetchone():
            raise HTTPException(404, "no such vendor")
        # Holds and payments reference the vendor, so they go first or the FK bites.
        c.execute("DELETE FROM payment WHERE vendor_id=?", (vendor_id,))
        c.execute(
            "DELETE FROM verification WHERE hold_id IN (SELECT id FROM hold WHERE vendor_id=?)",
            (vendor_id,),
        )
        c.execute("DELETE FROM recording_receipt WHERE hold_id IN (SELECT id FROM hold WHERE vendor_id=?)", (vendor_id,))
        c.execute("DELETE FROM hold WHERE vendor_id=?", (vendor_id,))
        c.execute("DELETE FROM vendor WHERE id=?", (vendor_id,))
        c.commit()
        db.log(c, "user", "vendor_deleted", vendor_id, "")
        return {"ok": True}


@app.get("/api/status")
def status():
    return {
        "nemotron": "live" if config.have_nemotron() else "offline (deterministic fallback)",
        "elevenlabs": "live" if config.have_elevenlabs() else "simulated",
        "hold_threshold": config.HOLD_THRESHOLD,
        "demo_mode": config.DEMO_MODE,
    }


@app.post("/api/reset", dependencies=[Depends(require_demo)])
def reset():
    """Reseed and reprocess the demo inbox. Safe to hit between demo runs."""
    # Truncate rather than unlink. Deleting the file out from under other open
    # connections silently loses rows -- it dropped a whole message, and with it
    # the demo, the first time this ran concurrently.
    llm.STATS.reset()
    with conn() as c:
        db.clear(c)
        telemetry.clear()
        db.seed(c)
        messages = inbox.load()

        def run(m: dict) -> dict:
            own = db.connect()  # sqlite connections are not shareable across threads
            try:
                d = process(own, m)
                return {
                    "id": m["id"],
                    "role": m.get("demo_role"),
                    "held": d.held,
                    "score": d.assessment.score,
                    "source": d.assessment.source,
                }
            finally:
                own.close()

        with ThreadPoolExecutor(max_workers=min(8, len(messages))) as pool:
            results = list(pool.map(run, messages))

        order = {m["id"]: i for i, m in enumerate(messages)}
        results.sort(key=lambda r: order[r["id"]])
        return {"processed": results, "llm": {"succeeded": llm.STATS.succeeded, "fell_back": llm.STATS.failed}}


@app.get("/api/board")
def board():
    """The queue as the clerk thinks about it.

    open      — still needs something to happen
      needs_review  awaiting verification; flagged when we cannot tell who sent it
      calling       a verification call is in flight
      escalated     the call resolved nothing; a human has to decide
    settled   — accepted (cleared, or the change confirmed) / denied (blocked)
    extraneous — mail that was never about money
    """
    with conn(ensure_seeded=True) as c:
        rows = c.execute(
            """SELECT h.*, m.sender, m.subject, m.received_at, m.id AS msg_id,
                      v.name AS vendor_name, v.phone_on_file
               FROM hold h JOIN message m ON m.id = h.message_id
               LEFT JOIN vendor v ON v.id = h.vendor_id
               ORDER BY h.score DESC, h.created_at DESC"""
        ).fetchall()

        def item(r):
            d = dict(r)
            ver = c.execute(
                "SELECT judgment, reply_source, created_at FROM verification "
                "WHERE hold_id=? AND judgment IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT 1", (d["id"],)
            ).fetchone()
            sigs = json.loads(d["signals"])
            inferred = any(s["key"] == "inferred_identity" and s["fired"] for s in sigs)
            return {
                "id": d["id"], "kind": "hold",
                "vendor_name": d["vendor_name"], "vendor_id": d["vendor_id"],
                "identified": bool(d["vendor_id"]) and not inferred,
                "inferred": inferred,
                "sender": d["sender"], "subject": d["subject"],
                "received_at": d["received_at"], "score": d["score"],
                "rationale": d["rationale"], "status": d["status"],
                "phone_on_file": d["phone_on_file"],
                "judgment": ver["judgment"] if ver else None,
                "reply_source": ver["reply_source"] if ver else None,
            }

        items = [item(r) for r in rows]
        open_ = [i for i in items if i["status"] in ("held", "calling", "verifying", "escalated")]

        # A routine invoice that cleared is a payment we accepted without friction.
        cleared = [
            {"id": m["id"], "kind": "message", "vendor_name": None, "identified": True,
             "sender": m["sender"], "subject": m["subject"], "received_at": m["received_at"],
             "score": 0, "rationale": "Cleared — ready for payment; no payment has been sent.",
             "status": "cleared", "judgment": None}
            for m in c.execute(
                "SELECT id, sender, subject, received_at FROM message "
                "WHERE status='cleared' ORDER BY received_at DESC"
            ).fetchall()
        ]
        extraneous = [
            {"id": m["id"], "kind": "message", "sender": m["sender"], "subject": m["subject"],
             "received_at": m["received_at"], "status": "extraneous",
             "rationale": "Not about money — never reached the fraud checks."}
            for m in c.execute(
                "SELECT id, sender, subject, received_at FROM message "
                "WHERE status='extraneous' ORDER BY received_at DESC"
            ).fetchall()
        ]

        return {
            "open": {
                "needs_review": [i for i in open_ if i["status"] == "held"],
                "calling": [i for i in open_ if i["status"] in ("calling", "verifying")],
                "escalated": [i for i in open_ if i["status"] == "escalated"],
            },
            "settled": {
                "accepted": [i for i in items if i["status"] == "approved"] + cleared,
                "denied": [i for i in items if i["status"] == "blocked"],
            },
            "extraneous": extraneous,
        }


@app.get("/api/messages")
def messages():
    with conn(ensure_seeded=True) as c:
        rows = c.execute("SELECT id, received_at, sender, subject, status FROM message ORDER BY received_at").fetchall()
        return [dict(r) for r in rows]


@app.get("/api/holds")
def holds():
    with conn(ensure_seeded=True) as c:
        rows = c.execute(
            """SELECT h.*, m.sender, m.subject, m.received_at, v.name AS vendor_name,
                      v.phone_on_file, v.account AS vendor_account
               FROM hold h
               JOIN message m ON m.id = h.message_id
               LEFT JOIN vendor v ON v.id = h.vendor_id
               ORDER BY h.score DESC, h.created_at DESC"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["signals"] = [s for s in json.loads(d["signals"]) if s["fired"]]
            ver = c.execute(
                "SELECT * FROM verification WHERE hold_id=? AND judgment IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT 1", (d["id"],)
            ).fetchone()
            d["verification"] = dict(ver) if ver else None
            out.append(d)
        return out


@app.get("/api/holds/{hold_id}")
def hold_detail(hold_id: str):
    with conn(ensure_seeded=True) as c:
        r = c.execute(
            """SELECT h.*, m.sender, m.reply_to, m.subject, m.body, m.extracted,
                      v.name AS vendor_name, v.phone_on_file, v.account AS vendor_account
               FROM hold h JOIN message m ON m.id=h.message_id
               LEFT JOIN vendor v ON v.id=h.vendor_id WHERE h.id=?""",
            (hold_id,),
        ).fetchone()
        if not r:
            raise HTTPException(404, "no such hold")
        d = dict(r)
        d["signals"] = json.loads(d["signals"])
        d["extracted"] = json.loads(d["extracted"]) if d["extracted"] else None
        ver = c.execute("SELECT * FROM verification WHERE hold_id=? AND judgment IS NOT NULL ORDER BY created_at DESC, rowid DESC", (hold_id,)).fetchall()
        d["verifications"] = [dict(v) for v in ver]
        return d


@app.post("/api/holds/{hold_id}/call", dependencies=[Depends(require_demo)])
def open_call(hold_id: str):
    """Start the callback: render the agent's question so the clerk can hear it.

    Returns the number we will dial and, with a key set, audio of the agent
    speaking. The vendor's reply is submitted separately to /reply.
    """
    with conn(ensure_seeded=True) as c:
        hold = c.execute("SELECT * FROM hold WHERE id=?", (hold_id,)).fetchone()
        if hold is None:
            raise HTTPException(404, "no such hold")
        if not hold["vendor_id"]:
            raise HTTPException(400, "cannot verify: no vendor on file to call")

        vendor = dict(c.execute("SELECT * FROM vendor WHERE id=?", (hold["vendor_id"],)).fetchone())
        msg = c.execute("SELECT extracted FROM message WHERE id=?", (hold["message_id"],)).fetchone()
        ex = extractor.Extraction(**json.loads(msg["extracted"]))

        line = voice.script_for(vendor, ex)
        audio = None
        audio_inline = None
        if config.have_elevenlabs():
            try:
                audio = voice.synthesize(line, f"agent-{vendor['id']}")
                # Inline it as a data URI. On serverless the file lives in this
                # instance's /tmp, and the browser's follow-up GET may land on a
                # different instance and 404.
                raw = (config.ROOT / audio).read_bytes() if not config.SERVERLESS else (
                    Path(audio) if Path(audio).is_absolute() else config.ROOT / audio
                ).read_bytes()
                audio_inline = "data:audio/mpeg;base64," + base64.b64encode(raw).decode()
            except Exception:  # noqa: BLE001 - the clerk can still read the line
                audio = None

        return {
            "dialed_number": vendor["phone_on_file"],
            "contact_name": vendor["contact_name"],
            "agent_line": line,
            "agent_audio": audio,
            "agent_audio_inline": audio_inline,
            "stt_available": config.have_elevenlabs(),
        }


@app.post("/api/holds/{hold_id}/reply", dependencies=[Depends(require_demo)])
def submit_reply(
    hold_id: str,
    audio: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
):
    """Submit the vendor's side of the call, spoken or typed, and judge it."""
    with conn(ensure_seeded=True) as c:
        data = audio.file.read(config.MAX_AUDIO_BYTES + 1) if audio is not None else None
        if data and len(data) > config.MAX_AUDIO_BYTES:
            raise HTTPException(413, "Audio exceeds the 10 MiB limit")
        if audio is not None and not data:
            raise HTTPException(400, "Audio recording is empty")
        try:
            if data:
                return verify(c, hold_id, reply_audio=data, audio_filename=audio.filename or "reply.webm")
            if text and text.strip():
                return verify(c, hold_id, scripted_reply=text.strip())
            return verify(c, hold_id)  # deterministic seeded reply
        except KeyError:
            raise HTTPException(404, "no such hold") from None
        except voice.STTUnavailable as e:
            raise HTTPException(422, f"could not transcribe: {e}") from None
        except VerificationConflict as e:
            raise HTTPException(409, str(e)) from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None


@app.post("/api/holds/{hold_id}/verify", dependencies=[Depends(require_demo)])
def run_verification(hold_id: str, scripted_reply: str | None = None):
    """One-shot verification with the seeded reply. Kept for the scripted demo path."""
    with conn() as c:
        try:
            return verify(c, hold_id, scripted_reply=scripted_reply)
        except KeyError:
            raise HTTPException(404, "no such hold") from None
        except VerificationConflict as e:
            raise HTTPException(409, str(e)) from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None


# The order the pipeline actually runs in. Sorting these alphabetically put
# "judge" before "score", which reads as though we judge the call before we
# score the email -- backwards, and the page exists to explain the pipeline.
PIPELINE_ORDER = {"extract": 0, "score": 1, "tts": 2, "stt": 3, "judge": 4}


@app.get("/api/activity")
def activity():
    """Every external service call, so a judge can see the models actually working."""
    rows = telemetry.recent(200)
    agg: dict = {}
    for r in rows:
        key = (r["service"], r["job"])
        a = agg.setdefault(
            "%s:%s" % key,
            {"service": r["service"], "job": r["job"], "calls": 0, "ok": 0,
             "failed": 0, "total_ms": 0, "units": 0, "models": {}},
        )
        a["calls"] += 1
        a["ok" if r["ok"] else "failed"] += 1
        a["total_ms"] += r["ms"] or 0
        a["units"] += r["units"] or 0
        if r["model"]:
            a["models"][r["model"]] = a["models"].get(r["model"], 0) + 1
    for a in agg.values():
        a["avg_ms"] = round(a["total_ms"] / a["calls"]) if a["calls"] else 0

    # An attempt is not a request. Retries are deliberate, and counting each one
    # as a failure made a working pipeline look like it was collapsing. Group by
    # req_id and report what the caller actually experienced.
    by_req: dict = {}
    for r in rows:
        rid = r.get("req_id") or f"solo-{r['id']}"
        g = by_req.setdefault(rid, {"job": r["job"], "service": r["service"],
                                    "attempts": 0, "ok": False, "ms": 0})
        g["attempts"] += 1
        g["ms"] += r["ms"] or 0
        g["ok"] = g["ok"] or bool(r["ok"])

    reqs = list(by_req.values())
    nem = [g for g in reqs if g["service"] == "nemotron"]
    outcome = {
        "requests": len(nem),
        "first_try": sum(1 for g in nem if g["ok"] and g["attempts"] == 1),
        "recovered": sum(1 for g in nem if g["ok"] and g["attempts"] > 1),
        "failed": sum(1 for g in nem if not g["ok"]),
        "attempts": sum(g["attempts"] for g in nem),
    }

    tts_chars = sum(r["units"] or 0 for r in rows if r["job"] == "tts" and r["ok"])
    return {
        "calls": rows,
        "summary": sorted(agg.values(), key=lambda a: PIPELINE_ORDER.get(a["job"], 99)),
        "outcome": outcome,
        "config": {
            "nemotron_chain": config.NEMOTRON_MODELS,
            "nemotron_live": config.have_nemotron(),
            "elevenlabs_live": config.have_elevenlabs(),
            "voice_id": config.ELEVENLABS_VOICE_ID,
            "timeout_s": config.LLM_TIMEOUT,
        },
        "elevenlabs_chars_used_this_session": tts_chars,
        "dispatch": {
            "max_inflight": llm.MAX_INFLIGHT,
            "per_model_inflight": llm.PER_MODEL_INFLIGHT,
            "min_spacing_s": llm.MIN_SPACING,
            "model_health": llm.health(),
        },
    }


@app.get("/api/audit")
def audit():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100").fetchall()]


@app.get("/api/recording/{name}")
def recording(name: str):
    path = config.RECORDINGS / Path(name).name
    if not path.exists():
        raise HTTPException(404, "no recording")
    media = "audio/webm" if path.suffix in (".webm", ".ogg") else "audio/mpeg"
    return FileResponse(path, media_type=media)


if WEB.exists():
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
