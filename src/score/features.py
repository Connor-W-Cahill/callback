"""Deterministic risk features.

These are computed in Python, not asked of a model, so that "how did it decide?"
has a real answer. Nemotron weighs them and writes the rationale; it does not
invent the evidence.
"""
from dataclasses import dataclass
from datetime import date, datetime

from src.score.resolve import domain_of, similarity


@dataclass
class Signal:
    key: str
    fired: bool
    weight: float
    detail: str


# Payment runs go out on the 1st and 15th. A change landing just before one
# is timed to catch money in motion.
PAYMENT_RUN_DAYS = (1, 15)


def compute(
    *,
    extraction,
    vendor: dict | None,
    history: list[dict],
    message: dict,
    match_evidence: dict,
    today: date | None = None,
) -> list[Signal]:
    today = today or date.today()
    sig: list[Signal] = []
    sender = message.get("sender", "")
    reply_to = message.get("reply_to") or sender

    # --- the hardest signal: account differs from what we have paid before ---
    new_account = bool(
        extraction.bank_account
        and vendor
        and extraction.bank_account != vendor["account"]
    )
    sig.append(
        Signal(
            "account_change",
            new_account,
            0.40,
            (
                f"remit-to account ending {extraction.bank_account[-4:]} differs from the "
                f"account used in all {len(history)} prior payments to this vendor"
            )
            if new_account and extraction.bank_account
            else "remit-to account matches the vendor master",
        )
    )

    # --- lookalike domain ---
    lookalike = match_evidence.get("matched_on") == "lookalike_domain"
    detail = "sender domain matches the vendor master"
    if lookalike and vendor:
        detail = (
            f"sender domain {domain_of(sender)} is a near-miss of "
            f"{vendor['domains'][0]} (similarity {match_evidence.get('domain_similarity')})"
        )
    elif match_evidence.get("matched_on") == "none":
        detail = f"sender domain {domain_of(sender)} matches no known vendor"
    sig.append(Signal("lookalike_domain", lookalike, 0.25, detail))

    # --- unresolvable vendor ---
    matched_on = match_evidence.get("matched_on", "")
    inferred = matched_on.startswith("inferred")
    if vendor is None:
        detail = "could not match this sender to any vendor on file"
    elif inferred:
        conf = match_evidence.get("match_confidence", 0)
        detail = (
            f"sender is not on file for {vendor['name']}; identified from content "
            f"({match_evidence.get('match_evidence', '')}) at {conf:.0%} confidence"
        )
    else:
        detail = f"resolved to {vendor['name']} via {matched_on}"
    sig.append(Signal("unknown_vendor", vendor is None, 0.20, detail))

    # --- identified only by inference ---
    # Knowing who they *probably* are is not knowing who they are. If the bank
    # details also changed, an inferred identity is exactly the shape of an
    # attacker mailing from an address we have never seen.
    sig.append(
        Signal(
            "inferred_identity",
            inferred,
            0.15,
            f"vendor inferred from message content, not from a known sender or domain"
            if inferred
            else "vendor identified from the sender itself",
        )
    )

    # --- reply-to redirect ---
    redirect = bool(reply_to and domain_of(reply_to) != domain_of(sender))
    sig.append(
        Signal(
            "reply_to_mismatch",
            redirect,
            0.20,
            f"reply-to {reply_to} points to a different domain than the sender"
            if redirect
            else "reply-to matches the sending domain",
        )
    )

    # --- first contact from this address ---
    unknown_sender = bool(
        vendor and sender and sender.lower() not in [s.lower() for s in vendor["known_senders"]]
    )
    sig.append(
        Signal(
            "new_sender_address",
            unknown_sender,
            0.15,
            f"first message we have seen from {sender}; this vendor has written from "
            f"{len(vendor['known_senders'])} other addresses"
            if unknown_sender and vendor
            else "sender address is one we have corresponded with before",
        )
    )

    # --- discouraging verification: attacker pre-empting the callback ---
    sig.append(
        Signal(
            "discourages_verification",
            extraction.discourages_verification,
            0.25,
            "message discourages phoning to verify"
            if extraction.discourages_verification
            else "message does not discourage verification",
        )
    )

    # --- urgency ---
    urgent = extraction.urgency == "high"
    sig.append(
        Signal(
            "urgency",
            urgent,
            0.10,
            "message applies deadline pressure"
            if urgent
            else f"urgency reads as {extraction.urgency}",
        )
    )

    # --- amount anomaly ---
    anomaly, adetail = _amount_anomaly(extraction.amount, history)
    sig.append(Signal("amount_anomaly", anomaly, 0.10, adetail))

    # --- timing against the payment run ---
    near_run = _days_to_run(today) <= 3
    sig.append(
        Signal(
            "near_payment_run",
            near_run and extraction.requests_payment_change,
            0.10,
            f"change requested {_days_to_run(today)} days before a scheduled payment run"
            if near_run
            else "not close to a scheduled payment run",
        )
    )

    # --- off-profile bank country ---
    off_country = bool(
        extraction.bank_country
        and vendor
        and extraction.bank_country not in ("US", None)
        and vendor["country"] == "US"
    )
    sig.append(
        Signal(
            "bank_country_mismatch",
            off_country,
            0.20,
            f"remit-to country {extraction.bank_country} is inconsistent with this "
            f"vendor's {vendor['country']} profile"
            if off_country and vendor
            else "remit-to country is consistent with the vendor profile",
        )
    )

    return sig


def _amount_anomaly(amount: float | None, history: list[dict]) -> tuple[bool, str]:
    if amount is None or len(history) < 3:
        return False, "not enough history to judge the amount"
    amounts = [p["amount"] for p in history]
    lo, hi = min(amounts), max(amounts)
    if amount > hi * 1.5:
        return True, f"${amount:,.2f} is well above this vendor's historical high of ${hi:,.2f}"
    if amount < lo * 0.4:
        return True, f"${amount:,.2f} is well below this vendor's historical low of ${lo:,.2f}"
    return False, f"${amount:,.2f} sits within this vendor's usual ${lo:,.0f}-${hi:,.0f} range"


def _days_to_run(today: date) -> int:
    best = 99
    for d in PAYMENT_RUN_DAYS:
        if d >= today.day:
            best = min(best, d - today.day)
    # next month's first run
    import calendar

    days_in_month = calendar.monthrange(today.year, today.month)[1]
    best = min(best, (days_in_month - today.day) + PAYMENT_RUN_DAYS[0])
    return best


def base_score(signals: list[Signal]) -> float:
    """Weighted sum, squashed to 0-1. The model may adjust this, but this is the floor."""
    total = sum(s.weight for s in signals if s.fired)
    return round(min(1.0, total), 3)
