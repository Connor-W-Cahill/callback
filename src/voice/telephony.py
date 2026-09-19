"""Twilio outbound calling: make the verification callback a real phone call.

The security property is unchanged and non-negotiable -- we dial the number from
the VENDOR MASTER, never one supplied in the email. Twilio only changes the
transport.

Nothing here runs unless TWILIO_* is configured AND auto-calling is explicitly
enabled. Placing real phone calls is the one action in this project with
consequences outside the laptop, so it is off by default.
"""
import hmac
import base64
import hashlib
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from src import config, telemetry

API = "https://api.twilio.com/2010-04-01"


class TelephonyError(RuntimeError):
    pass


@dataclass
class CallHandle:
    sid: str
    to: str
    status: str


def configured() -> bool:
    return bool(config.TWILIO_SID and config.TWILIO_TOKEN and config.TWILIO_FROM and config.PUBLIC_BASE_URL)


def twiml_for(agent_audio_url: str | None, agent_line: str, action_url: str) -> str:
    """What Twilio says and does once the vendor picks up.

    Plays the ElevenLabs render if we have a public URL for it, falls back to
    Twilio's own TTS, then records the answer and posts it back to us.
    """
    speak = (
        f'<Play>{_esc(agent_audio_url)}</Play>'
        if agent_audio_url
        else f'<Say voice="Polly.Joanna">{_esc(agent_line)}</Say>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"{speak}"
        '<Say voice="Polly.Joanna">Please answer after the tone.</Say>'
        f'<Record maxLength="20" playBeep="true" trim="trim-silence" timeout="4" '
        f'action="{_esc(action_url)}" recordingStatusCallback="{_esc(action_url)}" '
        f'recordingStatusCallbackEvent="completed" />'
        '<Say voice="Polly.Joanna">We did not hear a response. Goodbye.</Say>'
        "</Response>"
    )


def place_call(to: str, twiml: str, status_callback: str | None = None) -> CallHandle:
    """Dial `to` and run the given TwiML. `to` comes from the vendor master."""
    if not configured():
        raise TelephonyError("Twilio is not configured")
    if not to or not to.startswith("+"):
        raise TelephonyError(f"refusing to dial {to!r}: need E.164, e.g. +14125550142")

    data = {"To": to, "From": config.TWILIO_FROM, "Twiml": twiml}
    if status_callback:
        data["StatusCallback"] = status_callback

    import time as _t

    t0 = _t.time()
    r = httpx.post(
        f"{API}/Accounts/{config.TWILIO_SID}/Calls.json",
        data=data,
        auth=(config.TWILIO_SID, config.TWILIO_TOKEN),
        timeout=30,
    )
    ok = r.status_code in (200, 201)
    telemetry.record(
        service="twilio", job="call", model=config.TWILIO_FROM, ok=ok,
        ms=int((_t.time() - t0) * 1000),
        detail="" if ok else r.text[:250],
        prompt_excerpt=f"dialing {to} (number on file)",
        response_excerpt=r.text[:300] if ok else "",
    )
    if not ok:
        raise TelephonyError(f"Twilio rejected the call: {r.status_code} {r.text[:200]}")
    b = r.json()
    return CallHandle(sid=b.get("sid", ""), to=to, status=b.get("status", ""))


def fetch_recording(recording_url: str) -> bytes:
    """Download a finished recording. Twilio needs the account auth here too."""
    url = recording_url if recording_url.endswith(".wav") else recording_url + ".wav"
    r = httpx.get(url, auth=(config.TWILIO_SID, config.TWILIO_TOKEN), timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.content


def valid_signature(url: str, params: dict, signature: str) -> bool:
    """Verify a webhook really came from Twilio.

    The endpoint is public and acts on what it receives, so an unsigned request
    could drive the fraud control. Reject anything that does not verify.
    """
    if not config.TWILIO_TOKEN or not signature:
        return False
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(config.TWILIO_TOKEN.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def _esc(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )
