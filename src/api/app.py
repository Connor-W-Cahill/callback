"""Backend for the AP clerk's hold queue."""
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src import config, db
from src.ingest import inbox
from src.pipeline import process, verify

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
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    c = conn()
    db.seed(c)
    results = []
    for m in inbox.load():
        d = process(c, m)
        results.append(
            {"id": m["id"], "role": m.get("demo_role"), "held": d.held, "score": d.assessment.score}
        )
    return {"processed": results}


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
            "SELECT * FROM verification WHERE hold_id=? ORDER BY created_at DESC LIMIT 1", (d["id"],)
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
    ver = c.execute("SELECT * FROM verification WHERE hold_id=? ORDER BY created_at DESC", (hold_id,)).fetchall()
    d["verifications"] = [dict(v) for v in ver]
    return d


@app.post("/api/holds/{hold_id}/verify")
def run_verification(hold_id: str, scripted_reply: str | None = None):
    c = conn()
    try:
        return verify(c, hold_id, scripted_reply=scripted_reply)
    except KeyError:
        raise HTTPException(404, "no such hold") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/audit")
def audit():
    c = conn()
    return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100").fetchall()]


@app.get("/api/recording/{name}")
def recording(name: str):
    path = config.RECORDINGS / Path(name).name
    if not path.exists():
        raise HTTPException(404, "no recording")
    return FileResponse(path, media_type="audio/mpeg")


if WEB.exists():
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
