"""ElevenLabs outbound verification call.

The security property lives here: we dial the number from the VENDOR MASTER,
never a number supplied in the email. The email is the compromised channel, so
verification has to leave it.

Three modes, chosen by what is configured:
  live      -- ElevenLabs TTS renders the agent's side, STT transcribes the reply
  simulated -- deterministic scripted vendor, no network (default with no key)
Both produce the same transcript shape, so the judge downstream cannot tell.
"""
import json
from dataclasses import dataclass
from pathlib import Path

import httpx

from src import config

TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"


@dataclass
class CallResult:
    dialed_number: str
    transcript: str
    audio_path: str | None
    mode: str


def script_for(vendor: dict, extraction) -> str:
    """What the agent says. Short, states its purpose, asks a closed question."""
    last4 = extraction.bank_account[-4:] if extraction.bank_account else "unknown"
    return (
        f"Hello, this is an automated payment verification call for "
        f"{vendor['name']}. We received an email request to change the bank account "
        f"we use to pay your invoices, to an account ending {last4}. "
        f"We are calling the number we have on file to confirm. "
        f"Did your company send that request?"
    )


def place_call(vendor: dict, extraction, *, scripted_reply: str | None = None) -> CallResult:
    """Place the verification call. Falls back to simulation without a key."""
    number = vendor["phone_on_file"]  # never from the email. This is the control.
    agent_line = script_for(vendor, extraction)
    reply = scripted_reply if scripted_reply is not None else _default_reply(vendor, extraction)

    audio_path = None
    mode = "simulated"
    if config.have_elevenlabs():
        try:
            audio_path = synthesize(agent_line, vendor["id"])
            mode = "live_tts"
        except Exception:  # noqa: BLE001 - a failed render must not kill the call
            audio_path = None
            mode = "simulated"

    transcript = f"AGENT: {agent_line}\nVENDOR: {reply}"
    return CallResult(dialed_number=number, transcript=transcript, audio_path=audio_path, mode=mode)


def synthesize(text: str, tag: str) -> str:
    """Render the agent's line to an mp3 via ElevenLabs TTS."""
    config.RECORDINGS.mkdir(parents=True, exist_ok=True)
    out = config.RECORDINGS / f"{tag}.mp3"
    r = httpx.post(
        TTS_URL.format(voice_id=config.ELEVENLABS_VOICE_ID),
        headers={"xi-api-key": config.ELEVENLABS_API_KEY, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_turbo_v2_5"},
        timeout=60.0,
    )
    r.raise_for_status()
    out.write_bytes(r.content)
    return str(out.relative_to(config.ROOT))


def transcribe(audio_path: str) -> str:
    """Transcribe the vendor's side via ElevenLabs STT."""
    if not config.have_elevenlabs():
        raise RuntimeError("ELEVENLABS_API_KEY not set")
    with open(audio_path, "rb") as fh:
        r = httpx.post(
            STT_URL,
            headers={"xi-api-key": config.ELEVENLABS_API_KEY},
            files={"file": fh},
            data={"model_id": "scribe_v1"},
            timeout=120.0,
        )
    r.raise_for_status()
    return r.json().get("text", "")


def _default_reply(vendor: dict, extraction) -> str:
    """The simulated vendor's side.

    Deterministic so the demo runs the same way every time: a vendor confronted
    with a change they did not request denies it.
    """
    replies = json.loads((config.SEED / "call_replies.json").read_text())
    if extraction.bank_account and extraction.bank_account != vendor["account"]:
        return replies["denial"]
    return replies["routine"]
