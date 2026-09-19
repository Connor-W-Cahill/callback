"""ElevenLabs outbound verification call.

The security property lives here: we dial the number from the VENDOR MASTER,
never a number supplied in the email. The email is the compromised channel, so
verification has to leave it.

How the vendor's side is captured, in descending order of realism:
  spoken    -- a human speaks into a mic, ElevenLabs STT transcribes it (option 2)
  typed     -- a human types the reply; used when no key is set
  scripted  -- canned reply from the seed data, for a deterministic demo run
All three produce the same transcript shape, so the judge downstream cannot tell
how the words were captured -- which is what makes the fallbacks safe.
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


def place_call(
    vendor: dict,
    extraction,
    *,
    scripted_reply: str | None = None,
    reply_source: str = "scripted",
) -> CallResult:
    """Assemble the verification call. Renders the agent's side if a key is set."""
    number = vendor["phone_on_file"]  # never from the email. This is the control.
    agent_line = script_for(vendor, extraction)
    reply = scripted_reply if scripted_reply is not None else _default_reply(vendor, extraction)

    audio_path = None
    if config.have_elevenlabs():
        try:
            audio_path = synthesize(agent_line, f"agent-{vendor['id']}")
        except Exception:  # noqa: BLE001 - a failed render must not kill the call
            audio_path = None

    transcript = f"AGENT: {agent_line}\nVENDOR: {reply}"
    return CallResult(
        dialed_number=number,
        transcript=transcript,
        audio_path=audio_path,
        mode=reply_source,
    )


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


class STTUnavailable(RuntimeError):
    """Raised when we cannot turn the vendor's audio into words."""


def transcribe(data: bytes, filename: str = "reply.webm", *, keep_as: str | None = None) -> tuple[str, str | None]:
    """Transcribe the vendor's spoken reply via ElevenLabs STT.

    Returns (text, saved_audio_path). The audio is kept so the clerk -- and the
    judges -- can replay exactly what was said.
    """
    if not config.have_elevenlabs():
        raise STTUnavailable("ELEVENLABS_API_KEY not set")
    if not data:
        raise STTUnavailable("no audio received")

    saved = None
    if keep_as:
        config.RECORDINGS.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix or ".webm"
        out = config.RECORDINGS / f"{keep_as}{suffix}"
        out.write_bytes(data)
        saved = str(out.relative_to(config.ROOT))

    try:
        r = httpx.post(
            STT_URL,
            headers={"xi-api-key": config.ELEVENLABS_API_KEY},
            files={"file": (filename, data)},
            data={"model_id": "scribe_v1"},
            timeout=120.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise STTUnavailable(f"speech-to-text failed: {e}") from e

    text = (r.json() or {}).get("text", "").strip()
    if not text:
        raise STTUnavailable("speech-to-text returned no words")
    return text, saved


def _default_reply(vendor: dict, extraction) -> str:
    """The simulated vendor's side.

    Deterministic so the demo runs the same way every time: a vendor confronted
    with a change they did not request denies it.
    """
    replies = json.loads((config.SEED / "call_replies.json").read_text())
    if extraction.bank_account and extraction.bank_account != vendor["account"]:
        return replies["denial"]
    return replies["routine"]
