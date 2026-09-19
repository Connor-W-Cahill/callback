"""Vercel entrypoint. The Python runtime serves the ASGI app exported here."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.app import app  # noqa: E402,F401
