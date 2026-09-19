"""The pipeline: inbound message -> extract -> resolve -> score -> decide -> verify.

Deliberately linear. The value is in the specific checks, and a readable pipeline
is easier to explain to a judge than an agent loop.
"""
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from src import config, db
from src.extract import extractor
from src.judge import judge as judging
from src.score import features, resolve, scorer, vendor_match
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
    match_confidence: float = 1.0
    matched_on: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def process(conn: sqlite3.Connection, message: dict, *, use_llm: bool = True, today: date | None = None) -> Decision:
    """Run one inbound message through the pipeline and persist the outcome."""
    vendors = db.vendors(conn)

    extract_fn = extractor.extract if use_llm else extractor.extract_rules
    ex = extract_fn(message)
    # Independent rules remain evidence even when the model returns valid JSON.
    rules = extractor.extract_rules(message)
    ex.requests_payment_change |= rules.requests_payment_change
    ex.payment_related |= rules.payment_related
    ex.bank_account = ex.bank_account or rules.bank_account
    ex.bank_routing = ex.bank_routing or rules.bank_routing

    vendor, evidence = resolve.resolve(
        vendors, sender=message.get("sender", ""), claimed_name=ex.vendor_name or message.get("claimed_vendor")
    )

    # Deterministic resolution needs a known sender, domain or near-exact name.
    # When it finds nothing, fall back to identifying the vendor from what the
    # email actually talks about. A match this way is a LEAD, not an identity:
    # evidence records how weak it is so the rest of the pipeline can treat it
    # with suspicion rather than trust.
    if vendor is None:
        text = extractor._message_text(message)
        m = vendor_match.match(text, vendors, use_llm=use_llm)
        if m.vendor:
            vendor = m.vendor
            evidence.update(
                matched_on=f"inferred_{m.source}",
                match_confidence=m.confidence,
                match_evidence=m.evidence,
                match_terms=m.terms,
            )
    history = db.payments_for(conn, vendor["id"]) if vendor else []
    sig = features.compute(
        extraction=ex, vendor=vendor, history=history, message=message, match_evidence=evidence, today=today
    )

    # A model must not hide a changed number by extracting a different one.
    for key, field, current in (("account_change", "bank_account", "account"),
                                ("routing_change", "bank_routing", "routing")):
        value = getattr(rules, field)
        if vendor and value and value != vendor[current]:
            for signal in sig:
                if signal.key == key:
                    signal.fired = True
                    signal.detail = f"{current} in the message differs from the vendor master"
            if getattr(ex, field) != value:
                ex.requests_payment_change = True
                ex.payment_details_unclear = True
                # Conflicting extracted payment instructions need a human.
                setattr(ex, field, None)

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
        s.key in ("account_change", "routing_change") and s.fired for s in sig
    )
    held = policy_hold or assessment.score >= config.HOLD_THRESHOLD
    reason = (
        "payment-detail change requires callback verification"
        if policy_hold
        else ("risk score above threshold" if held else "cleared")
    )

    hold_id = None
    with conn:
        _save_message(conn, message, ex)
        if not ex.payment_related and not policy_hold:
            held, reason = False, "not payment related"
            conn.execute("UPDATE message SET status='extraneous' WHERE id=?", (message["id"],))
            db.log(conn, "system", "message_extraneous", message["id"], reason, commit=False)
        elif held:
            hold_id = _save_hold(conn, message, vendor, assessment, sig)
            db.log(conn, "system", "hold_created", hold_id, reason, commit=False)
        else:
            conn.execute("UPDATE message SET status='cleared' WHERE id=?", (message["id"],))
            db.log(conn, "system", "message_cleared", message["id"], assessment.rationale, commit=False)

    return Decision(
        message["id"], vendor, ex, sig, assessment, held, reason, hold_id,
        match_confidence=float(evidence.get("match_confidence", 1.0)),
        matched_on=str(evidence.get("matched_on", "")),
    )


class VerificationConflict(ValueError):
    """The hold is settled, already being verified, or changed during the call."""


def verify(
    conn: sqlite3.Connection,
    hold_id: str,
    *,
    scripted_reply: str | None = None,
    reply_audio: bytes | None = None,
    audio_filename: str = "reply.webm",
    recording_sid: str | None = None,
    call_sid: str | None = None,
) -> dict:
    """Claim once, perform external work, then atomically persist the decision."""
    if not config.DEMO_MODE and not (recording_sid and call_sid and reply_audio):
        raise ValueError("live verification requires a signed phone-call recording")
    vid = f"ver-{uuid.uuid4().hex}"
    with conn:
        # The write obtains the SQLite lock before we read the claimed state.
        changed = conn.execute(
            "UPDATE hold SET status='verifying' WHERE id=? "
            "AND status IN ('held', 'calling', 'escalated') "
            "AND (? IS NULL OR call_sid=?)",
            (hold_id, call_sid, call_sid),
        ).rowcount
        if not changed:
            if not conn.execute("SELECT 1 FROM hold WHERE id=?", (hold_id,)).fetchone():
                raise KeyError(hold_id)
            raise VerificationConflict("hold is settled, busy, or belongs to another call")
        hold = conn.execute("SELECT * FROM hold WHERE id=?", (hold_id,)).fetchone()
        if not hold["vendor_id"]:
            raise ValueError("cannot verify: no vendor on file to call")
        vendor = dict(conn.execute("SELECT * FROM vendor WHERE id=?", (hold["vendor_id"],)).fetchone())
        msg = conn.execute("SELECT * FROM message WHERE id=?", (hold["message_id"],)).fetchone()
        ex = extractor.Extraction(**json.loads(msg["extracted"]))
        if recording_sid:
            try:
                conn.execute("INSERT INTO recording_receipt VALUES (?,?)", (recording_sid, hold_id))
            except sqlite3.IntegrityError as e:
                raise VerificationConflict("recording already processed") from e
        conn.execute(
            "INSERT INTO verification (id, hold_id, dialed_number, recording_sid, created_at) "
            "VALUES (?,?,?,?,?)",
            (vid, hold_id, vendor["phone_on_file"], recording_sid, _now()),
        )

    try:
        reply_audio_path = None
        reply_source = "scripted"
        if reply_audio is not None:
            scripted_reply, reply_audio_path = voice.transcribe(
                reply_audio, audio_filename, keep_as=f"vendor-{vid}"
            )
            reply_source = "phone" if recording_sid else "spoken"
        elif scripted_reply is not None:
            reply_source = "typed"
        call = voice.place_call(vendor, ex, scripted_reply=scripted_reply, reply_source=reply_source)
        verdict = judging.judge(call.transcript)
        if verdict.judgment == "confirmed" and (
            ex.payment_details_unclear or
            (ex.requests_payment_change and not (ex.bank_account or ex.bank_routing))
        ):
            verdict = judging.Judgment("unclear", verdict.quote,
                "Payment details are missing or conflicting; human review required.", "policy")
        status = {"denied": "blocked", "confirmed": "approved", "unclear": "escalated"}[verdict.judgment]
        with conn:
            changed = conn.execute(
                "UPDATE hold SET status=? WHERE id=? AND status='verifying' "
                "AND EXISTS (SELECT 1 FROM verification WHERE id=? AND judgment IS NULL)",
                (status, hold_id, vid),
            ).rowcount
            if not changed:
                raise VerificationConflict("hold changed during verification")
            current = conn.execute("SELECT * FROM vendor WHERE id=?", (vendor["id"],)).fetchone()
            if not current or any(current[k] != vendor[k] for k in ("account", "routing", "phone_on_file")):
                raise VerificationConflict("vendor details changed during verification; retry required")
            conn.execute(
                "UPDATE verification SET transcript=?, audio_path=?, judgment=?, judge_quote=?, "
                "judge_reasoning=?, reply_audio_path=?, reply_source=?, created_at=? WHERE id=?",
                (call.transcript, call.audio_path, verdict.judgment, verdict.quote,
                 verdict.reasoning, reply_audio_path, reply_source, _now(), vid),
            )
            db.log(conn, "voice_agent", f"callback_{verdict.judgment}", hold_id,
                   f"on-file number {call.dialed_number}; source {reply_source}; {verdict.reasoning}",
                   commit=False)
            if verdict.judgment == "confirmed" and (ex.bank_account or ex.bank_routing):
                conn.execute(
                    "UPDATE vendor SET account=COALESCE(?, account), routing=COALESCE(?, routing), "
                    "custom=1 WHERE id=?", (ex.bank_account, ex.bank_routing, vendor["id"]),
                )
                db.log(conn, "system", "vendor_bank_updated", vendor["id"],
                       "Verified payment details updated", commit=False)
        return {
            "verification_id": vid, "dialed_number": call.dialed_number,
            "mode": call.mode, "transcript": call.transcript, "audio_path": call.audio_path,
            "reply_audio_path": reply_audio_path, "reply_source": reply_source,
            "judgment": verdict.judgment, "quote": verdict.quote,
            "reasoning": verdict.reasoning, "judge_source": verdict.source, "hold_status": status,
        }
    except Exception:
        with conn:
            changed = conn.execute("DELETE FROM verification WHERE id=? AND judgment IS NULL", (vid,)).rowcount
            if changed:
                conn.execute("UPDATE hold SET status='escalated' WHERE id=? AND status='verifying'", (hold_id,))
                if recording_sid:
                    conn.execute("DELETE FROM recording_receipt WHERE recording_sid=?", (recording_sid,))
                db.log(conn, "system", "verification_failed", hold_id,
                       "Verification failed; payment remains held and can be retried", commit=False)
        raise


def _save_message(conn: sqlite3.Connection, message: dict, ex: extractor.Extraction) -> None:
    conn.execute(
        """INSERT INTO message
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
    return hold_id
