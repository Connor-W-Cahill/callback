"""Backend for the AP clerk's hold queue."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src import config, db, llm, telemetry
from src.extract import extractor
from src.ingest import inbox
from src.pipeline import process, verify
from src.voice import agent as voice

app = FastAPI(title="Callback", description="Vendor payment-change fraud interceptor")
WEB = config.ROOT / "web"


def conn():
    c = db.connect()
    db.init(c)
    return c


@app.get("/api/status")
def status():
    return {
        "nemotron": "live" if config.have_nemotron() else "offline (deterministic fallback)",
        "elevenlabs": "live" if config.have_elevenlabs() else "simulated",
        "hold_threshold": config.HOLD_THRESHOLD,
    }


@app.post("/api/reset")
def reset():
    """Reseed and reprocess the demo inbox. Safe to hit between demo runs."""
    # Truncate rather than unlink. Deleting the file out from under other open
    # connections silently loses rows -- it dropped a whole message, and with it
    # the demo, the first time this ran concurrently.
    llm.STATS.reset()
    c = conn()
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


@app.get("/api/messages")
def messages():
    c = conn()
    rows = c.execute("SELECT id, received_at, sender, subject, status FROM message ORDER BY received_at").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/holds")
def holds():
    c = conn()
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
            "SELECT * FROM verification WHERE hold_id=? ORDER BY id DESC LIMIT 1", (d["id"],)
        ).fetchone()
        d["verification"] = dict(ver) if ver else None
        out.append(d)
    return out


@app.get("/api/holds/{hold_id}")
def hold_detail(hold_id: str):
    c = conn()
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
    ver = c.execute("SELECT * FROM verification WHERE hold_id=? ORDER BY id DESC", (hold_id,)).fetchall()
    d["verifications"] = [dict(v) for v in ver]
    return d


@app.post("/api/holds/{hold_id}/call")
def open_call(hold_id: str):
    """Start the callback: render the agent's question so the clerk can hear it.

    Returns the number we will dial and, with a key set, audio of the agent
    speaking. The vendor's reply is submitted separately to /reply.
    """
    c = conn()
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
    if config.have_elevenlabs():
        try:
            audio = voice.synthesize(line, f"agent-{vendor['id']}")
        except Exception:  # noqa: BLE001 - the clerk can still read the line
            audio = None

    return {
        "dialed_number": vendor["phone_on_file"],
        "contact_name": vendor["contact_name"],
        "agent_line": line,
        "agent_audio": audio,
        "stt_available": config.have_elevenlabs(),
    }


@app.post("/api/holds/{hold_id}/reply")
async def submit_reply(
    hold_id: str,
    audio: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
):
    """Submit the vendor's side of the call, spoken or typed, and judge it."""
    c = conn()
    data = await audio.read() if audio is not None else None
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
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/holds/{hold_id}/verify")
def run_verification(hold_id: str, scripted_reply: str | None = None):
    """One-shot verification with the seeded reply. Kept for the scripted demo path."""
    c = conn()
    try:
        return verify(c, hold_id, scripted_reply=scripted_reply)
    except KeyError:
        raise HTTPException(404, "no such hold") from None
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

    tts_chars = sum(r["units"] or 0 for r in rows if r["job"] == "tts" and r["ok"])
    return {
        "calls": rows,
        "summary": sorted(agg.values(), key=lambda a: PIPELINE_ORDER.get(a["job"], 99)),
        "config": {
            "nemotron_chain": config.NEMOTRON_MODELS,
            "nemotron_live": config.have_nemotron(),
            "elevenlabs_live": config.have_elevenlabs(),
            "voice_id": config.ELEVENLABS_VOICE_ID,
            "timeout_s": config.LLM_TIMEOUT,
        },
        "elevenlabs_chars_used_this_session": tts_chars,
    }


@app.get("/api/audit")
def audit():
    c = conn()
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
