"""Runtime config. Every external service is optional.

The pipeline runs end to end with no API keys at all, using deterministic
offline fallbacks. That keeps the demo alive when the wifi dies and lets a
teammate clone the repo and run it without waiting on credentials.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SEED = DATA / "seed"

# Serverless: the bundle is read-only and only /tmp is writable. /tmp is also
# per-instance and wiped on cold start, so state here is ephemeral by design --
# every visitor effectively gets their own freshly seeded demo.
SERVERLESS = bool(os.getenv("VERCEL"))
_STATE = Path("/tmp/callback") if SERVERLESS else ROOT

DB_PATH = Path(os.getenv("DATABASE_URL", "") or (_STATE / "data" / "callback.sqlite"))
RECORDINGS = _STATE / "recordings"

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
# Being in the public catalog does NOT mean your account can call it: most
# nemotron ids return 404 "not found for account". Individual models also go
# down mid-session (503, or timeouts under load), so this is a CHAIN: try each
# in order, then fall back to rules. Verified working by scripts/preflight.py.
NEMOTRON_MODELS = [
    m.strip()
    for m in os.getenv(
        "NEMOTRON_MODELS",
        "nvidia/nemotron-3-super-120b-a12b,"
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning,"
        "nvidia/nemotron-3.5-lightning-30b-a3b",
    ).split(",")
    if m.strip()
]
# Single-model override, kept because the eval's --model flag sets it.
NEMOTRON_MODEL = os.getenv("NEMOTRON_MODEL", "") or NEMOTRON_MODELS[0]

LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "12"))

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "eng")

# Any request touching payment details is verified regardless of score.
# This is policy, not a threshold -- see docs. HOLD_THRESHOLD only governs
# holds triggered by other signals.
HOLD_THRESHOLD = float(os.getenv("HOLD_THRESHOLD", "0.35"))


# --- Twilio (phase 4: the callback as a real phone call) -------------------
# Off unless all of these are set AND CALLBACK_AUTO_CALL=1. Real phone calls are
# the only action here with consequences off the laptop.
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM = os.getenv("TWILIO_FROM", "").strip()
# Where Twilio can reach us: a cloudflared/ngrok tunnel, or the Vercel URL.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
# Place the verification call automatically when inbound mail is held.
AUTO_CALL = os.getenv("CALLBACK_AUTO_CALL", "0") == "1"


def have_nemotron() -> bool:
    return bool(NVIDIA_API_KEY)


def have_elevenlabs() -> bool:
    return bool(ELEVENLABS_API_KEY)
