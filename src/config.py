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
# Verified against https://integrate.api.nvidia.com/v1/models (public, no auth).
# nano-3-30b is the fast default -- this pipeline makes 3 calls per message and
# demo latency matters. Swap to nemotron-3-super-120b-a12b for quality; the eval
# takes --model so you can show the comparison.
NEMOTRON_MODEL = os.getenv("NEMOTRON_MODEL", "nvidia/nemotron-nano-3-30b-a3b")

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
