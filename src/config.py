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
DB_PATH = Path(os.getenv("DATABASE_URL", DATA / "callback.sqlite"))
RECORDINGS = ROOT / "recordings"

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
# Being in the public catalog does NOT mean your account can call it: most
# nemotron ids return 404 "not found for account". Verified working on this
# account by scripts/preflight.py, which auto-discovers a substitute if the
# configured model is unavailable.
NEMOTRON_MODEL = os.getenv("NEMOTRON_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
# Reasoning models are slow to first token and cold-start badly. 45s was too tight.
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# Any request touching payment details is verified regardless of score.
# This is policy, not a threshold -- see docs. HOLD_THRESHOLD only governs
# holds triggered by other signals.
HOLD_THRESHOLD = float(os.getenv("HOLD_THRESHOLD", "0.35"))


def have_nemotron() -> bool:
    return bool(NVIDIA_API_KEY)


def have_elevenlabs() -> bool:
    return bool(ELEVENLABS_API_KEY)
