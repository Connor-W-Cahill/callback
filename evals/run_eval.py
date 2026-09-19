"""Run both evals and print the numbers that go on the slide.

  eval 1 -- fraud detection over labeled synthetic email
  eval 2 -- the transcript judge

Each runs in two conditions, rules-only and rules+Nemotron, so the model has to
earn its place rather than merely be present. With no NVIDIA_API_KEY set both
conditions collapse to rules and the comparison says so.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, db  # noqa: E402
from src.extract import extractor  # noqa: E402
from src.judge import judge as judging  # noqa: E402
from src.score import features, resolve, scorer  # noqa: E402

FRAUD_THRESHOLD = 0.60


def score_case(conn, case: dict, *, use_llm: bool) -> dict:
    msg = {
        "id": case["id"],
        "sender": case["sender"],
        "reply_to": case["reply_to"],
        "subject": case["subject"],
        "body": case["body"],
        "claimed_vendor": case["claimed_vendor"],
    }
    ex = extractor.extract(msg) if use_llm else extractor.extract_rules(msg)
    vendors = db.vendors(conn)
    vendor, evidence = resolve.resolve(vendors, sender=msg["sender"], claimed_name=case["claimed_vendor"])
    history = db.payments_for(conn, vendor["id"]) if vendor else []
    sig = features.compute(
        extraction=ex, vendor=vendor, history=history, message=msg, match_evidence=evidence
    )
    if use_llm:
        a = scorer.assess(sig, vendor=vendor, extraction=ex)
        score, source = a.score, a.source
    else:
        score, source = features.base_score(sig), "rules"

    held = ex.requests_payment_change or any(s.key == "account_change" and s.fired for s in sig)
    return {
        "id": case["id"],
        "tag": case["tag"],
        "truth": case["label"],
        "score": score,
        "held": held or score >= config.HOLD_THRESHOLD,
        "predicted_fraud": score >= FRAUD_THRESHOLD,
        "source": source,
    }


def fraud_eval(conn, cases: list[dict], *, use_llm: bool) -> dict:
    rows = [score_case(conn, c, use_llm=use_llm) for c in cases]
    tp = sum(1 for r in rows if r["truth"] == "fraud" and r["predicted_fraud"])
    fn = sum(1 for r in rows if r["truth"] == "fraud" and not r["predicted_fraud"])
    fp = sum(1 for r in rows if r["truth"] != "fraud" and r["predicted_fraud"])
    tn = sum(1 for r in rows if r["truth"] != "fraud" and not r["predicted_fraud"])

    # The number that actually matters operationally: fraud that was never held
    # at all, i.e. money that would have moved.
    escaped = [r for r in rows if r["truth"] == "fraud" and not r["held"]]
    # And the cost side: legitimate changes stopped for a phone call.
    change_friction = [r for r in rows if r["truth"] == "legit_change" and r["held"]]
    routine_friction = [r for r in rows if r["truth"] == "legit" and r["held"]]

    return {
        "rows": rows,
        "tp": tp, "fn": fn, "fp": fp, "tn": tn,
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "escaped": escaped,
        "change_friction": len(change_friction),
        "routine_friction": routine_friction,
        "by_tag": _tag_recall(rows),
    }


def _tag_recall(rows) -> dict:
    out = {}
    for tag in sorted({r["tag"] for r in rows}):
        sub = [r for r in rows if r["tag"] == tag]
        frauds = [r for r in sub if r["truth"] == "fraud"]
        if frauds:
            out[tag] = (sum(1 for r in frauds if r["predicted_fraud"]), len(frauds))
    return out


def judge_eval(cases: list[dict], *, use_llm: bool) -> dict:
    rows = []
    for c in cases:
        v = judging.judge(c["transcript"]) if use_llm else judging.judge_rules(c["transcript"])
        rows.append({"id": c["id"], "truth": c["label"], "pred": v.judgment, "source": v.source})
    correct = sum(1 for r in rows if r["truth"] == r["pred"])
    # The unforgivable error: calling a denial a confirmation.
    catastrophic = [r for r in rows if r["truth"] == "denied" and r["pred"] == "confirmed"]
    return {
        "rows": rows,
        "accuracy": correct / len(rows) if rows else 0.0,
        "catastrophic": catastrophic,
        "confusion": Counter((r["truth"], r["pred"]) for r in rows),
    }


def bar(label, value, width=28):
    filled = int(round(value * width))
    return f"  {label:24s} {'█' * filled}{'░' * (width - filled)} {value:.0%}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = ap.parse_args()

    emails = json.loads((Path(__file__).parent / "cases" / "emails.json").read_text())
    transcripts = json.loads((Path(__file__).parent / "transcripts" / "cases.json").read_text())

    conn = db.connect(":memory:")
    db.init(conn)
    db.seed(conn)

    live = config.have_nemotron()
    conditions = [("rules only", False)] + ([("rules + nemotron", True)] if live else [])

    results = {}
    print("=" * 66)
    print(f"EVAL 1 — fraud detection   ({len(emails)} labeled synthetic emails)")
    print("=" * 66)
    for name, use_llm in conditions:
        r = fraud_eval(conn, emails, use_llm=use_llm)
        results[name] = r
        print(f"\n{name.upper()}")
        print(f"  confusion   TP {r['tp']}   FN {r['fn']}   FP {r['fp']}   TN {r['tn']}")
        print(bar("precision", r["precision"]))
        print(bar("recall", r["recall"]))
        print(f"\n  recall by attack type:")
        for tag, (hit, tot) in r["by_tag"].items():
            print(f"    {tag:22s} {hit}/{tot}")
        print(f"\n  fraud that escaped the hold entirely: {len(r['escaped'])}  <- the number that matters")
        print(f"  legitimate changes held for a call:   {r['change_friction']}  (cost: one phone call each)")
        print(f"  routine invoices wrongly held:        {len(r['routine_friction'])}")

    print()
    print("=" * 66)
    print(f"EVAL 2 — transcript judge   ({len(transcripts)} labeled call transcripts)")
    print("=" * 66)
    for name, use_llm in conditions:
        j = judge_eval(transcripts, use_llm=use_llm)
        results[f"judge:{name}"] = j
        print(f"\n{name.upper()}")
        print(bar("accuracy", j["accuracy"]))
        print(f"  denial misread as confirmation: {len(j['catastrophic'])}  <- must be 0")
        wrong = [r for r in j["rows"] if r["truth"] != r["pred"]]
        for w in wrong:
            print(f"    {w['id']}: labeled {w['truth']}, judged {w['pred']}")

    if not live:
        print()
        print("NOTE: NVIDIA_API_KEY is not set, so only the rules condition ran.")
        print("      Set it and re-run to produce the rules-vs-Nemotron comparison.")

    if args.json:
        Path("evals/results.json").write_text(
            json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in results.items()},
                       indent=2, default=str)
        )


if __name__ == "__main__":
    main()
