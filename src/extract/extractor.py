"""Nemotron job 1: unstructured vendor email -> structured fields.

Falls back to a regex extractor when the model is unavailable. The fallback is
deliberately decent: it is also the control arm in the eval (rules vs. rules+model).
"""
import re
from dataclasses import dataclass, field, asdict
from typing import Any

from src import llm

SYSTEM = """You extract structured data from accounts-payable email for a fraud control system.
Return ONLY a JSON object, no prose. Schema:
{
  "vendor_name": string or null,
  "invoice_number": string or null,
  "amount": number or null,
  "due_date": string or null,
  "bank_account": string or null,
  "bank_routing": string or null,
  "bank_country": string or null,
  "requests_payment_change": boolean,
  "discourages_verification": boolean,
  "urgency": "none" | "low" | "high"
}
requests_payment_change is true if the message asks to update, change, or newly
specify where payment should be sent.
discourages_verification is true if the message discourages calling, says the sender
is unavailable by phone, or asks to handle it only over email.
Use null when a field is genuinely absent. Do not invent values."""

ACCOUNT_RE = re.compile(r"(?:acct|account)\D{0,20}(\d[\d\s-]{6,19}\d)", re.I)
ROUTING_RE = re.compile(r"(?:routing|aba|rtn)\D{0,20}(\d{9})", re.I)
AMOUNT_RE = re.compile(r"\$\s?([\d,]+\.\d{2}|[\d,]+)")
INVOICE_RE = re.compile(r"\b(INV[-\s]?\d{3,6})\b", re.I)

# A change request needs a change VERB attached to a payment NOUN. Matching the
# noun alone flags every routine invoice that spells out its remit-to details.
CHANGE_VERBS = r"(chang\w*|updat\w*|switch\w*|migrat\w*|new|different|revis\w*|amend\w*)"
CHANGE_NOUNS = r"(bank\w*|account|remittance|remit-to|payment details|banking details|ach details|wire details)"
CHANGE_RE = re.compile(
    rf"\b{CHANGE_VERBS}\b[^.!?\n]{{0,40}}?\b{CHANGE_NOUNS}\b"
    rf"|\b{CHANGE_NOUNS}\b[^.!?\n]{{0,40}}?\b(have|has|had|were|was)?\s*{CHANGE_VERBS}\b",
    re.I,
)
DISCOURAGE_PHRASES = [
    "unable to take calls", "don't call", "do not call", "cannot take calls",
    "in meetings all day", "email only", "reach me by email", "phones are down",
    "traveling and unreachable", "no need to call",
]
URGENT_PHRASES = [
    "urgent", "immediately", "today", "asap", "end of day", "eod",
    "before close", "past due", "final notice", "avoid late fees",
]


@dataclass
class Extraction:
    vendor_name: str | None = None
    invoice_number: str | None = None
    amount: float | None = None
    due_date: str | None = None
    bank_account: str | None = None
    bank_routing: str | None = None
    bank_country: str | None = None
    requests_payment_change: bool = False
    discourages_verification: bool = False
    urgency: str = "none"
    source: str = "rules"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract(message: dict) -> Extraction:
    """Extract with Nemotron, falling back to rules on any failure."""
    text = _message_text(message)
    try:
        data = llm.complete_json(SYSTEM, text)
        return _from_model(data)
    except llm.LLMUnavailable:
        return extract_rules(message)


def extract_rules(message: dict) -> Extraction:
    """Deterministic extractor. Control arm for the eval, fallback in production."""
    text = _message_text(message)
    low = text.lower()

    account = _clean_digits(ACCOUNT_RE.search(text))
    routing = _clean_digits(ROUTING_RE.search(text))

    amount = None
    m = AMOUNT_RE.search(text)
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            amount = None

    inv = INVOICE_RE.search(text)

    requests_change = bool(CHANGE_RE.search(text))

    urgency = "none"
    hits = sum(1 for p in URGENT_PHRASES if p in low)
    if hits >= 2:
        urgency = "high"
    elif hits == 1:
        urgency = "low"

    country = "US"
    for name, code in (("united kingdom", "GB"), ("hong kong", "HK"), ("iban", "EU"), ("swift", "INTL")):
        if name in low:
            country = code
            break

    return Extraction(
        vendor_name=message.get("claimed_vendor"),
        invoice_number=inv.group(1).upper().replace(" ", "-") if inv else None,
        amount=amount,
        bank_account=account,
        bank_routing=routing,
        bank_country=country,
        requests_payment_change=requests_change,
        discourages_verification=any(p in low for p in DISCOURAGE_PHRASES),
        urgency=urgency,
        source="rules",
    )


def _from_model(data: dict) -> Extraction:
    amount = data.get("amount")
    if isinstance(amount, str):
        try:
            amount = float(amount.replace("$", "").replace(",", ""))
        except ValueError:
            amount = None

    return Extraction(
        vendor_name=data.get("vendor_name"),
        invoice_number=data.get("invoice_number"),
        amount=amount,
        due_date=data.get("due_date"),
        bank_account=_digits_only(data.get("bank_account")),
        bank_routing=_digits_only(data.get("bank_routing")),
        bank_country=data.get("bank_country") or "US",
        requests_payment_change=bool(data.get("requests_payment_change")),
        discourages_verification=bool(data.get("discourages_verification")),
        urgency=data.get("urgency") or "none",
        source="nemotron",
    )


def _message_text(message: dict) -> str:
    parts = [
        f"From: {message.get('sender','')}",
        f"Reply-To: {message.get('reply_to') or message.get('sender','')}",
        f"Subject: {message.get('subject','')}",
        "",
        message.get("body", ""),
    ]
    if message.get("attachment_text"):
        parts += ["", "--- attachment ---", message["attachment_text"]]
    return "\n".join(parts)


def _clean_digits(m: re.Match | None) -> str | None:
    if not m:
        return None
    return re.sub(r"\D", "", m.group(1)) or None


def _digits_only(v: Any) -> str | None:
    if not v:
        return None
    d = re.sub(r"\D", "", str(v))
    return d or None
