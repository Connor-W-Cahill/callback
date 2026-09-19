"""The pipeline: inbound message -> extract -> resolve -> score -> decide -> verify.

Deliberately linear. The value is in the specific checks, and a readable pipeline
is easier to explain to a judge than an agent loop.
"""
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from src import config, db
from src.extract import extractor
from src.judge import judge as judging
from src.score import features, resolve, scorer
from src.voice import agent as voice


@dataclass
class Decision:
    message_id: str
    vendor: dict | None
    extraction: extractor.Extraction
    signals: list[features.Signal]
    assessment: scorer.Assessment
    held: bool
    reason: str
    hold_id: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def process(conn: sqlite3.Connection, message: dict, *, use_llm: bool = True) -> Decision:
    """Run one inbound message through the pipeline and persist the outcome."""
    vendors = db.vendors(conn)

    extract_fn = extractor.extract if use_llm else extractor.extract_rules
    ex = extract_fn(message)

    vendor, evidence = resolve.resolve(
        vendors, sender=message.get("sender", ""), claimed_name=ex.vendor_name or message.get("claimed_vendor")
    )
    history = db.payments_for(conn, vendor["id"]) if vendor else []
    sig = features.compute(
        extraction=ex, vendor=vendor, history=history, message=message, match_evidence=evidence
    )

    assessment = (
        scorer.assess(sig, vendor=vendor, extraction=ex)
        if use_llm
        else scorer.Assessment(
            score=features.base_score(sig),
            rationale=scorer.write_rationale(sig),
            top_signals=[s.key for s in sig if s.fired][:4],
            source="rules",
        )
    )

    # POLICY, not a threshold: any request to change where money goes is verified.
    # A model is never given the opportunity to wave one through.
    policy_hold = ex.requests_payment_change or any(
        s.key == "account_change" and s.fired for s in sig
    )
    held = policy_hold or assessment.score >= config.HOLD_THRESHOLD
    reason = (
        "payment-detail change requires callback verification"
        if policy_hold
        else ("risk score above threshold" if held else "cleared")
    )

    _save_message(conn, message, ex)
    hold_id = None
    if held:
        hold_id = _save_hold(conn, message, vendor, assessment, sig)
        db.log(conn, "system", "hold_created", hold_id, reason)
    else:
        conn.execute("UPDATE message SET status='cleared' WHERE id=?", (message["id"],))
        conn.commit()
        db.log(conn, "system", "message_cleared", message["id"], assessment.rationale)

    return Decision(message["id"], vendor, ex, sig, assessment, held, reason, hold_id)


def verify(
    conn: sqlite3.Connection,
    hold_id: str,
    *,
    scripted_reply: str | None = None,
    reply_audio: bytes | None = None,
    audio_filename: str = "reply.webm",
) -> dict:
    """Place the callback for a held payment and judge what the vendor said.

    The vendor's side can arrive three ways, all ending in the same transcript:
      reply_audio     -- a human spoke; ElevenLabs STT turns it into words
      scripted_reply  -- a human typed it, or the demo supplied a canned line
      neither         -- the deterministic seeded reply
    """
    hold = conn.execute("SELECT * FROM hold WHERE id=?", (hold_id,)).fetchone()
    if hold is None:
        raise KeyError(hold_id)
    msg = conn.execute("SELECT * FROM message WHERE id=?", (hold["message_id"],)).fetchone()
    ex = extractor.Extraction(**json.loads(msg["extracted"]))

    if not hold["vendor_id"]:
        raise ValueError("cannot verify: no vendor on file to call")
    vrow = conn.execute("SELECT * FROM vendor WHERE id=?", (hold["vendor_id"],)).fetchone()
    vendor = dict(vrow)

    reply_audio_path = None
    reply_source = "scripted"
    if reply_audio is not None:
        # A real human just spoke. This is the whole point: the words the judge
        # reads were never written down by us.
        scripted_reply, reply_audio_path = voice.transcribe(
            reply_audio, audio_filename, keep_as=f"vendor-{hold_id}"
        )
        reply_source = "spoken"
    elif scripted_reply is not None:
        reply_source = "typed"

    call = voice.place_call(vendor, ex, scripted_reply=scripted_reply, reply_source=reply_source)
    verdict = judging.judge(call.transcript)

    vid = f"ver-{uuid.uuid4().hex[:8]}"
    conn.execute(
        """INSERT INTO verification
           (id, hold_id, dialed_number, transcript, audio_path, judgment, judge_quote,
            judge_reasoning, reply_audio_path, reply_source, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            vid,
            hold_id,
            call.dialed_number,
            call.transcript,
            call.audio_path,
            verdict.judgment,
            verdict.quote,
            verdict.reasoning,
            reply_audio_path,
            reply_source,
            _now(),
        ),
    )

    status = {"denied": "blocked", "confirmed": "approved", "unclear": "escalated"}[verdict.judgment]
    conn.execute("UPDATE hold SET status=? WHERE id=?", (status, hold_id))
    conn.commit()

    db.log(
        conn,
        "voice_agent",
        f"callback_{verdict.judgment}",
        hold_id,
        f"dialed {call.dialed_number} (on-file number); reply captured via "
        f"{reply_source}; {verdict.reasoning}",
    )

    # A confirmed change is the only path that writes to the vendor master.
    if verdict.judgment == "confirmed" and ex.bank_account:
        conn.execute(
            "UPDATE vendor SET account=?, routing=COALESCE(?, routing) WHERE id=?",
            (ex.bank_account, ex.bank_routing, vendor["id"]),
        )
        conn.commit()
        db.log(conn, "system", "vendor_bank_updated", vendor["id"], f"account now ends {ex.bank_account[-4:]}")

    return {
        "verification_id": vid,
        "dialed_number": call.dialed_number,
        "mode": call.mode,
        "transcript": call.transcript,
        "audio_path": call.audio_path,
        "reply_audio_path": reply_audio_path,
        "reply_source": reply_source,
        "judgment": verdict.judgment,
        "quote": verdict.quote,
        "reasoning": verdict.reasoning,
        "judge_source": verdict.source,
        "hold_status": status,
    }


def _save_message(conn: sqlite3.Connection, message: dict, ex: extractor.Extraction) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO message
           (id, received_at, sender, reply_to, subject, body, attachment_text, extracted, status)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            message["id"],
            message.get("received_at", _now()),
            message.get("sender", ""),
            message.get("reply_to"),
            message.get("subject", ""),
            message.get("body", ""),
            message.get("attachment_text"),
            json.dumps(ex.to_dict()),
            "processed",
        ),
    )
    conn.commit()


def _save_hold(conn, message, vendor, assessment, signals) -> str:
    # Derived from the message, not random. On serverless each instance seeds
    # its own /tmp, and a random id minted on one container 404s on the next.
    hold_id = f"hold-{message['id']}"
    conn.execute(
        """INSERT INTO hold (id, message_id, vendor_id, score, rationale, signals, status, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            hold_id,
            message["id"],
            vendor["id"] if vendor else None,
            assessment.score,
            assessment.rationale,
            json.dumps([{"key": s.key, "fired": s.fired, "weight": s.weight, "detail": s.detail} for s in signals]),
            "held",
            _now(),
        ),
    )
    conn.execute("UPDATE message SET status='held' WHERE id=?", (message["id"],))
    conn.commit()
    return hold_id
