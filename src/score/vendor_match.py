"""Identify the vendor when the email never spells out the company name.

Deterministic resolution needs a known sender, a known domain, or something
close to the registered name. Real mail is looser: "Denise here from Allegheny",
"re: the refrigerant order", a signature block with only a first name. This
module covers that gap in two layers:

  keywords  distinctive terms from the vendor master, weighted by how rare each
            term is across vendors. Runs always, costs nothing, explains itself.
  nemotron  reads the email against a compact vendor list and picks one, for the
            cases keywords cannot reach ("the sheet metal people").

A guess is never treated as an identification. Both layers return a confidence,
and the caller decides what a low one is allowed to do.
"""
import re
from collections import Counter
from dataclasses import dataclass, field

from src import llm

# Words that appear in half the vendor names and identify nobody.
STOPWORDS = {
    "inc", "llc", "ltd", "co", "corp", "company", "group", "partners", "services",
    "service", "supply", "wholesale", "the", "and", "of", "solutions", "systems",
    "industries", "enterprises", "associates", "holdings",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "vendor_id": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "evidence": {"type": "string"},
    },
    "required": ["vendor_id", "confidence", "evidence"],
}

SYSTEM = """You identify which supplier an accounts-payable email is from.

You are given a list of vendors on file and the text of an email. Pick the ONE
vendor the email is most plausibly from, or null if nothing fits.

The company name is often not written out. Go on whatever is there: a product or
trade the vendor deals in, a contact's first name, a partial name, a place, an
invoice prefix, the subject matter.

Return ONLY this JSON:
{"vendor_id": string or null, "confidence": number 0-1, "evidence": "the words that led you there"}

Confidence means: 0.9+ the email names them or their contact unambiguously;
0.6-0.9 strong circumstantial fit; 0.3-0.6 a plausible guess; below 0.3 return null.
Guessing wrong is worse than returning null -- a misidentified vendor means we
compare bank details against the wrong record. When two vendors fit equally,
return null."""


@dataclass
class Match:
    vendor: dict | None
    confidence: float
    evidence: str
    source: str
    terms: list[str] = field(default_factory=list)


def tokens_for(vendor: dict) -> set[str]:
    """Distinctive terms that would make you think of this vendor."""
    bag: set[str] = set()
    for part in re.split(r"[^a-z0-9]+", vendor["name"].lower()):
        if part and part not in STOPWORDS and len(part) > 2:
            bag.add(part)
    for d in vendor.get("domains", []):
        root = d.split(".")[0]
        for part in re.split(r"[^a-z0-9]+", root):
            if part and part not in STOPWORDS and len(part) > 3:
                bag.add(part)
    if vendor.get("contact_name"):
        for part in vendor["contact_name"].lower().split():
            if len(part) > 2:
                bag.add(part)
    for extra in (vendor.get("category"), vendor.get("bank_name")):
        if extra:
            for part in re.split(r"[^a-z0-9]+", extra.lower()):
                if part and part not in STOPWORDS and len(part) > 3:
                    bag.add(part)
    return bag


def keyword_match(text: str, vendors: list[dict]) -> Match:
    """Score vendors by distinctive-term overlap, weighting rare terms higher."""
    low = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "
    index = {v["id"]: tokens_for(v) for v in vendors}

    # A term shared by several vendors says little; one unique to a vendor says a lot.
    freq = Counter(t for toks in index.values() for t in toks)

    best, best_score, best_terms = None, 0.0, []
    for v in vendors:
        hits = [t for t in index[v["id"]] if f" {t} " in low]
        if not hits:
            continue
        score = sum(1.0 / freq[t] for t in hits)
        if score > best_score:
            best, best_score, best_terms = v, score, sorted(hits, key=lambda t: freq[t])

    if not best:
        return Match(None, 0.0, "no vendor terms appear in the message", "keywords")

    # Two unique terms is a solid identification; one is suggestive.
    confidence = min(0.85, 0.35 * best_score)
    return Match(
        best, round(confidence, 2),
        f"message mentions {', '.join(repr(t) for t in best_terms[:3])}",
        "keywords", best_terms,
    )


def model_match(text: str, vendors: list[dict]) -> Match:
    """Nemotron reads the email against the vendor list. Used when keywords stall."""
    listing = "\n".join(
        f"- {v['id']}: {v['name']}"
        + (f" (contact {v['contact_name']})" if v.get("contact_name") else "")
        + (f", deals in {v['category']}" if v.get("category") else "")
        + (f", domain {v['domains'][0]}" if v.get("domains") else "")
        for v in vendors
    )
    try:
        data = llm.complete_json(
            SYSTEM, f"Vendors on file:\n{listing}\n\nEmail:\n{text[:2500]}",
            job="vendor_match", schema=SCHEMA,
        )
    except llm.LLMUnavailable:
        return Match(None, 0.0, "model unavailable", "rules")

    vid = data.get("vendor_id")
    conf = float(data.get("confidence") or 0)
    vendor = next((v for v in vendors if v["id"] == vid), None)
    if not vendor or conf < 0.3:
        return Match(None, 0.0, str(data.get("evidence", ""))[:200], "nemotron")
    return Match(vendor, round(min(conf, 0.95), 2), str(data.get("evidence", ""))[:200], "nemotron")


def match(text: str, vendors: list[dict], *, use_llm: bool = True) -> Match:
    """Best effort identification. Keywords first; the model only if they stall."""
    kw = keyword_match(text, vendors)
    if kw.vendor and kw.confidence >= 0.6:
        return kw
    if use_llm:
        m = model_match(text, vendors)
        if m.vendor and m.confidence > kw.confidence:
            return m
    return kw
