"""Match a claimed vendor against the vendor master.

Attackers use near-miss names and domains, so matching is fuzzy and the
*quality* of the match is itself a risk signal rather than a yes/no.
"""
import re
from difflib import SequenceMatcher

# Characters that read alike in a proxied font. Normalising these catches
# rn/m and l/1 style lookalike domains.
HOMOGLYPHS = str.maketrans({"1": "l", "0": "o", "5": "s", "$": "s"})


def norm(s: str) -> str:
    s = (s or "").lower().translate(HOMOGLYPHS)
    s = s.replace("rn", "m")
    return re.sub(r"[^a-z0-9]", "", s)


def domain_of(email: str) -> str:
    return (email or "").split("@")[-1].strip().lower()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def resolve(vendors: list[dict], *, sender: str, claimed_name: str | None) -> tuple[dict | None, dict]:
    """Return (vendor, evidence). Evidence explains how we matched, for the rationale."""
    sender_domain = domain_of(sender)
    evidence: dict = {
        "sender_domain": sender_domain,
        "matched_on": None,
        "domain_similarity": 0.0,
        "name_similarity": 0.0,
    }

    # 1. Exact known sender address -- the strongest match.
    for v in vendors:
        if sender and sender.lower() in [s.lower() for s in v["known_senders"]]:
            evidence["matched_on"] = "known_sender"
            evidence["domain_similarity"] = 1.0
            return v, evidence

    # 2. Exact domain, unknown address at that domain.
    for v in vendors:
        if sender_domain and sender_domain in [d.lower() for d in v["domains"]]:
            evidence["matched_on"] = "known_domain"
            evidence["domain_similarity"] = 1.0
            return v, evidence

    # 3. Near-miss domain -- typosquatting. Match, but flag it loudly.
    best, best_score = None, 0.0
    for v in vendors:
        for d in v["domains"]:
            s = similarity(sender_domain, d)
            if s > best_score:
                best, best_score = v, s
    if best and best_score >= 0.80:
        evidence["matched_on"] = "lookalike_domain"
        evidence["domain_similarity"] = round(best_score, 3)
        return best, evidence

    # 4. Fall back to the claimed display name.
    if claimed_name:
        nbest, nscore = None, 0.0
        for v in vendors:
            s = similarity(claimed_name, v["name"])
            if s > nscore:
                nbest, nscore = v, s
        if nbest and nscore >= 0.75:
            evidence["matched_on"] = "name_only"
            evidence["name_similarity"] = round(nscore, 3)
            evidence["domain_similarity"] = round(best_score, 3)
            return nbest, evidence

    evidence["matched_on"] = "none"
    evidence["domain_similarity"] = round(best_score, 3)
    return None, evidence
