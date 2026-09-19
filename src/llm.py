"""Nemotron client.

Three jobs in this project, none of them conversational:
  1. extract  -- unstructured email to structured JSON
  2. score    -- weigh risk signals and write a rationale
  3. judge    -- read a call transcript, decide confirmed/denied/unclear

Every call goes through `complete_json`, which returns parsed JSON or raises.
Callers are expected to have a deterministic fallback; see each module.
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from src import config, telemetry


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class Stats:
    """Call outcomes, so a 'rules + Nemotron' claim cannot silently be rules.

    NVIDIA's free tier returns 503s and truncated bodies under load. Without
    counting, a fallback looks exactly like a model that agreed with the rules.
    """
    attempts: int = 0
    succeeded: int = 0
    failed: int = 0
    reasons: dict = field(default_factory=dict)
    used: dict = field(default_factory=dict)

    def note_fail(self, why: str) -> None:
        self.failed += 1
        key = why.split(":")[0][:48]
        self.reasons[key] = self.reasons.get(key, 0) + 1

    def reset(self) -> None:
        self.attempts = self.succeeded = self.failed = 0
        self.reasons = {}
        self.used = {}

    @property
    def fallback_rate(self) -> float:
        total = self.succeeded + self.failed
        return self.failed / total if total else 0.0


STATS = Stats()
RETRIES = int(os.getenv("LLM_RETRIES", "2"))


def complete_json(system: str, user: str, *, temperature: float = 0.0,
                  max_tokens: int = 900, job: str = "call",
                  schema: dict | None = None) -> dict[str, Any]:
    """Call Nemotron and parse a JSON object out of the reply.

    With a schema, the server constrains decoding to valid JSON. Without one,
    a reasoning model will happily deliberate in prose until it hits the token
    ceiling and returns no JSON at all -- which was our single largest source
    of failures on the score job.
    """
    if not config.have_nemotron():
        raise LLMUnavailable("NVIDIA_API_KEY not set")

    payload: dict[str, Any] = {
        "model": config.NEMOTRON_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": job, "schema": schema},
        }

    STATS.attempts += 1
    last = "unknown"

    # Walk the model chain. A model that 404s for this account, 503s, or hangs
    # should cost us one timeout, not the whole request.
    models = [config.NEMOTRON_MODEL] + [
        m for m in config.NEMOTRON_MODELS if m != config.NEMOTRON_MODEL
    ]
    for model in models:
        payload["model"] = model
        for attempt in range(RETRIES + 1):
            t0 = time.time()
            raw = ""
            try:
                r = httpx.post(
                    f"{config.NVIDIA_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {config.NVIDIA_API_KEY}"},
                    json=payload,
                    timeout=float(config.LLM_TIMEOUT),
                )
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"]
                out = _parse_json(raw)
                STATS.succeeded += 1
                STATS.used[model] = STATS.used.get(model, 0) + 1
                telemetry.record(
                    service="nemotron", job=job, model=model, ok=True,
                    ms=int((time.time() - t0) * 1000), units=len(raw),
                    prompt_excerpt=user, response_excerpt=raw,
                )
                return out
            except Exception as e:  # noqa: BLE001 - transient; retry, then next model
                last = f"{type(e).__name__} on {model.split('/')[-1]}: {e}"
                # Some models reject response_format outright. Drop it and let
                # the next attempt fall back to prose parsing.
                if "response_format" in payload and isinstance(e, httpx.HTTPStatusError) \
                        and e.response.status_code == 400:
                    payload.pop("response_format", None)
                telemetry.record(
                    service="nemotron", job=job, model=model, ok=False,
                    ms=int((time.time() - t0) * 1000), detail=last,
                    prompt_excerpt=user, response_excerpt=raw,
                )
                if attempt < RETRIES:
                    time.sleep(0.5)

    STATS.note_fail(last)
    raise LLMUnavailable(last)


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
