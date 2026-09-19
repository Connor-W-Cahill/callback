#!/usr/bin/env bash
# Start Callback. No API keys required -- runs with deterministic fallbacks.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/uvicorn src.api.app:app --reload --port 8000
