"""Nemotron job 3: read the callback transcript and decide what the vendor said.

This is model-as-judge. It returns confirmed / denied / unclear, and `unclear`
escalates to a human rather than guessing -- an ambiguous phone call is not
authorisation to move money.
"""
from dataclasses import dataclass
import re

from src import llm

SYSTEM = """You read transcripts of verification phone calls made by an
accounts-payable fraud control. An AP system received an emailed request to change
a vendor's bank details and phoned the vendor at the number already on file to check.

Decide what the vendor actually said. Return ONLY this JSON object:
{
  "judgment": "confirmed" | "denied" | "unclear",
  "quote": "the exact words from the transcript you relied on",
  "reasoning": "one short sentence"
}

confirmed -- the vendor clearly acknowledges sending the request AND confirms the new details.
denied    -- the vendor says they did not send it, or does not recognise the change.
unclear   -- anything else: hedging, wrong person, voicemail, confusion, partial answers,
             or a caller who cannot verify the details. When in doubt, choose unclear.
Never choose confirmed on a partial or hesitant answer."""

DENY_CUES = [
    "didn't send", "did not send", "never sent", "not us", "wasn't us", "was not us",
    "no idea", "don't know anything", "haven't changed", "have not changed",
    "still the same", "no change", "that's not our", "not our account", "fraud",
]
CONFIRM_CUES = [
    "yes we sent", "that was us", "we did send", "confirm, yes", "confirm yes", "that's correct",
    "correct, we", "yes that's right", "we switched", "we changed our bank",
]
UNCLEAR_CUES = [
    "voicemail", "leave a message", "not available", "wrong number",
    "i'd have to check", "let me look into", "i'm not sure", "not sure",
    "new here", "can you email", "i think so", "cannot confirm", "can't confirm",
    "cannot say", "can't say", "did not say", "didn't say",
]


SCHEMA = {
    "type": "object",
    "properties": {
        "judgment": {"type": "string", "enum": ["confirmed", "denied", "unclear"]},
        "quote": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["judgment", "quote", "reasoning"],
}


@dataclass
class Judgment:
    judgment: str
    quote: str
    reasoning: str
    source: str


def judge(transcript: str) -> Judgment:
    vendor_text = vendor_turns(transcript)
    if not vendor_text.strip():
        return Judgment("unclear", "", "no vendor statement in the transcript", "rules")
    safety = _safety_judgment(vendor_text)
    if safety:
        return safety
    try:
        data = llm.complete_json(SYSTEM, f"Vendor statement:\n\n{vendor_text}",
                                 job="judge", schema=SCHEMA)
        j = str(data.get("judgment", "")).lower().strip()
        if j not in ("confirmed", "denied", "unclear"):
            raise llm.LLMUnavailable(f"bad judgment {j!r}")
        if j == "confirmed" and judge_rules(transcript).judgment != "confirmed":
            return Judgment("unclear", "", "Model confirmation lacks explicit confirmation evidence; human review required.", "policy")
        return Judgment(
            judgment=j,
            quote=str(data.get("quote", ""))[:400],
            reasoning=str(data.get("reasoning", ""))[:300],
            source="nemotron",
        )
    except llm.LLMUnavailable:
        return judge_rules(transcript)


def vendor_turns(transcript: str) -> str:
    """Only what the VENDOR said.

    The agent's own script contains words like "confirm", so scanning the whole
    transcript lets the agent answer its own question. Found by eval case t-15.
    """
    lines = []
    in_vendor_turn = False
    for line in transcript.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z _-]*):\s*(.*)$", line)
        if match:
            in_vendor_turn = match.group(1).strip().upper() == "VENDOR"
            if in_vendor_turn:
                lines.append(match.group(2))
        elif in_vendor_turn:
            lines.append(line)
    return "\n".join(lines)


def judge_rules(transcript: str) -> Judgment:
    """Deterministic judge. Control arm for the eval, fallback in production.

    Ordering matters: an explicit denial outranks hedging, and hedging outranks
    a confirmation cue, because 'I think so' is not authorisation.
    """
    vendor_text = vendor_turns(transcript)
    safety = _safety_judgment(vendor_text)
    if safety:
        return safety

    low = vendor_text.lower()
    for cue in CONFIRM_CUES:
        if cue in low:
            return Judgment("confirmed", _line_with(vendor_text, cue), f"vendor said {cue!r}", "rules")
    return Judgment("unclear", "", "no clear confirmation or denial in the transcript", "rules")


def _safety_judgment(vendor_text: str) -> Judgment | None:
    low = vendor_text.lower()
    for cue in DENY_CUES:
        if cue in low:
            return Judgment("denied", _line_with(vendor_text, cue), f"vendor said {cue!r}", "rules")
    for cue in UNCLEAR_CUES:
        if cue in low:
            return Judgment("unclear", _line_with(vendor_text, cue), f"hedged or unavailable: {cue!r}", "rules")
    return None


def _line_with(transcript: str, cue: str) -> str:
    for line in transcript.splitlines():
        if cue in line.lower():
            return line.strip()[:400]
    return ""
