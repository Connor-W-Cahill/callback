"""Nemotron client.

Three jobs in this project, none of them conversational:
  1. extract  -- unstructured email to structured JSON
  2. score    -- weigh risk signals and write a rationale
  3. judge    -- read a call transcript, decide confirmed/denied/unclear

Every call goes through `complete_json`, which returns parsed JSON or raises.
Callers are expected to have a deterministic fallback; see each module.
"""
import json
import re
from typing import Any

import httpx

from src import config


class LLMUnavailable(RuntimeError):
    pass


def complete_json(system: str, user: str, *, temperature: float = 0.0, max_tokens: int = 900) -> dict[str, Any]:
    """Call Nemotron and parse a JSON object out of the reply."""
    if not config.have_nemotron():
        raise LLMUnavailable("NVIDIA_API_KEY not set")

    payload = {
        "model": config.NEMOTRON_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        r = httpx.post(
            f"{config.NVIDIA_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.NVIDIA_API_KEY}"},
            json=payload,
            timeout=45.0,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001 - any failure falls back to rules
        raise LLMUnavailable(str(e)) from e

    return _parse_json(text)


def _parse_json(text: str) -> dict[str, Any]:
    """Nemotron reasoning models like to think out loud. Take the last JSON object."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise LLMUnavailable(f"no JSON object in reply: {text[:200]!r}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMUnavailable(f"bad JSON: {e}") from e
