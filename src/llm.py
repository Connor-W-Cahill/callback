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
import random
import re
import threading
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

# Measured against the live endpoint over 16 real extract calls:
#   503 "Service temporarily overloaded" / "ResourceExhausted" fails in 0.1-0.3s
#   a real success takes 3-13s (super-120b median 4.1s, omni median 10.0s)
# So a 503 is nearly free to retry and almost always transient, while a timeout
# is expensive and means the model is saturated. They are handled differently.
RETRIES = int(os.getenv("LLM_RETRIES", "2"))

# /api/reset fires a call per message at once, and the account returned
# "ResourceExhausted: Worker local total" under that. Capping requests in flight
# turns those 503s into a short queue instead.
# Per-job routing, from measured behaviour rather than preference:
#   extract  super-120b is fastest (median 4.1s vs 10.0s) and obeys the schema.
#   score    super-120b IGNORES json_schema here and deliberates in prose until
#            it runs out of tokens ("We need to compute score based on signals.
#            Likely we sum weights of fired signals, maybe cap at 1? Let's...").
#            omni emits a think block then valid JSON, which parses cleanly.
#   judge    same shape as score -- reasoning first, so route it the same way.
JOB_MODELS = {
    # Only omni. super-120b was kept as a backstop here and earned nothing: it
    # reliably spends ~8s deliberating in prose and returns no JSON, so falling
    # through to it just delays the rules path that would have answered
    # correctly and instantly. The rules scorer IS the backstop for these two.
    "score": ["nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"],
    "judge": ["nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"],
    "vendor_match": ["nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
                     "nvidia/nemotron-3-super-120b-a12b"],
}

# Reasoning jobs need a longer budget: scoring and judging think before they
# answer, and measured medians are 15.8s against 9.9s for extraction. A 20s
# ceiling was cutting off work that was about to succeed.
JOB_TIMEOUT = {"score": 40.0, "judge": 40.0, "vendor_match": 30.0}

MAX_INFLIGHT = int(os.getenv("LLM_MAX_INFLIGHT", "3"))
_SEM = threading.Semaphore(MAX_INFLIGHT)

# One pooled client: every call was paying for a fresh TLS handshake.
_CLIENT = httpx.Client(
    limits=httpx.Limits(max_keepalive_connections=8, max_connections=16,
                        keepalive_expiry=120.0),
)


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

    base_payload: dict[str, Any] = {
        "model": config.NEMOTRON_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if schema:
        base_payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": job, "schema": schema},
        }

    STATS.attempts += 1
    last = "unknown"

    # Walk the model chain. A model that 404s for this account, 503s, or hangs
    # should cost us one timeout, not the whole request.
    # A job with an explicit route uses only that route: falling through to a
    # model measured to fail this prompt costs latency and buys nothing.
    if job in JOB_MODELS:
        models = list(JOB_MODELS[job])
    else:
        models = [config.NEMOTRON_MODEL]
        models += [m for m in config.NEMOTRON_MODELS if m not in models]
    for model in models:
        # Rebuild per model. Dropping response_format for one model that
        # rejects it must not silently disarm schema-constrained decoding for
        # the next -- that let a reasoning model deliberate in prose again and
        # brought back the "no JSON object in reply" failures.
        payload = dict(base_payload)
        payload["model"] = model
        for attempt in range(RETRIES + 1):
            raw = ""
            t0 = time.time()
            try:
                with _SEM:
                    # Start the clock inside the gate: queueing behind other
                    # requests is our latency, not the model's.
                    t0 = time.time()
                    r = _CLIENT.post(
                        f"{config.NVIDIA_BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {config.NVIDIA_API_KEY}"},
                        json=payload,
                        timeout=JOB_TIMEOUT.get(job, float(config.LLM_TIMEOUT)),
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
            except Exception as e:  # noqa: BLE001 - classified below
                last = f"{type(e).__name__} on {model.split('/')[-1]}: {e}"
                telemetry.record(
                    service="nemotron", job=job, model=model, ok=False,
                    ms=int((time.time() - t0) * 1000), detail=last,
                    prompt_excerpt=user, response_excerpt=raw,
                )
                status = getattr(getattr(e, "response", None), "status_code", None)

                # A model that rejects response_format will keep rejecting it.
                if status == 400 and "response_format" in payload:
                    payload.pop("response_format", None)
                    continue

                # Overload and rate limiting fail in milliseconds and clear on
                # their own, so several quick retries cost almost nothing.
                if status in (429, 500, 502, 503, 504):
                    if attempt < RETRIES:
                        time.sleep(0.35 * (2 ** attempt) + random.uniform(0, 0.25))
                        continue
                    break

                # A timeout means this model is saturated. Retrying spends the
                # whole budget again for the same answer -- go to the next model.
                if isinstance(e, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout)):
                    break

                if attempt < RETRIES:
                    time.sleep(0.35)

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
