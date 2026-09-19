"""Nemotron job 2: weigh the computed signals and write the clerk-facing rationale.

The deterministic score is the floor. The model can raise it and must explain it,
but it cannot talk the system out of verifying a payment-detail change -- that
decision is policy, made in pipeline.py.
"""
from dataclasses import dataclass

from src import llm
from src.score.features import Signal, base_score

SYSTEM = """You are the risk engine for an accounts-payable fraud control.
You are given precomputed signals about an inbound vendor email. Each signal is
already determined -- do not re-derive them and do not invent new evidence.

Weigh them and return ONLY this JSON object:
{
  "score": number between 0 and 1,
  "rationale": "one or two plain sentences an AP clerk can act on",
  "top_signals": ["signal_key", ...]
}

Write the rationale the way a colleague would explain it out loud: name the
specific evidence, no jargon, no hedging. Never exceed 40 words.
Business email compromise is the threat model: a stranger posing as a known
vendor to redirect a payment. Missing one is far worse than a false alarm."""


@dataclass
class Assessment:
    score: float
    rationale: str
    top_signals: list[str]
    source: str


def assess(signals: list[Signal], *, vendor: dict | None, extraction) -> Assessment:
    floor = base_score(signals)
    try:
        data = llm.complete_json(SYSTEM, _prompt(signals, vendor, extraction))
        score = float(data.get("score", floor))
        rationale = str(data.get("rationale", "")).strip()
        if not rationale:
            raise llm.LLMUnavailable("empty rationale")
        return Assessment(
            score=round(max(score, floor), 3),  # the model may raise, never lower
            rationale=rationale,
            top_signals=[str(s) for s in data.get("top_signals", [])][:4],
            source="nemotron",
        )
    except (llm.LLMUnavailable, ValueError, TypeError):
        return Assessment(
            score=floor,
            rationale=write_rationale(signals),
            top_signals=[s.key for s in signals if s.fired][:4],
            source="rules",
        )


def write_rationale(signals: list[Signal]) -> str:
    """Deterministic rationale. Control arm for the eval, fallback in production."""
    fired = sorted([s for s in signals if s.fired], key=lambda s: -s.weight)
    if not fired:
        return "Nothing unusual: sender, remit-to account and amount all match this vendor's history."
    lead = fired[0].detail
    rest = [s.detail for s in fired[1:3]]
    if rest:
        return f"Held because {lead}. Also: {'; '.join(rest)}."
    return f"Held because {lead}."


def _prompt(signals: list[Signal], vendor: dict | None, extraction) -> str:
    lines = [f"Vendor on file: {vendor['name'] if vendor else 'UNRESOLVED'}"]
    if vendor:
        lines.append(f"Account on file ends: {vendor['account'][-4:]}")
    if extraction.bank_account:
        lines.append(f"Account requested ends: {extraction.bank_account[-4:]}")
    lines.append(f"Requests payment change: {extraction.requests_payment_change}")
    lines.append("")
    lines.append("Signals (fired = the condition is true):")
    for s in signals:
        lines.append(f"- {s.key}: fired={s.fired} weight={s.weight} :: {s.detail}")
    lines.append("")
    lines.append(f"Deterministic floor score: {base_score(signals)}")
    return "\n".join(lines)
