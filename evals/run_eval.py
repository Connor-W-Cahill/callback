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
from contextlib import closing
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, db, llm, pipeline  # noqa: E402
from src.judge import judge as judging  # noqa: E402

FRAUD_THRESHOLD = 0.60
EVAL_DATE = date(2026, 9, 19)


def score_case(conn, case: dict, *, use_llm: bool) -> dict:
    msg = {
        "id": case["id"],
        "sender": case["sender"],
        "reply_to": case["reply_to"],
        "subject": case["subject"],
        "body": case["body"],
        "claimed_vendor": case["claimed_vendor"],
    }
    decision = pipeline.process(conn, msg, use_llm=use_llm, today=EVAL_DATE)
    return {
        "id": case["id"],
        "tag": case["tag"],
        "truth": case["label"],
        "score": decision.assessment.score,
        "held": decision.held,
        "predicted_fraud": decision.assessment.score >= FRAUD_THRESHOLD,
        "source": decision.assessment.source,
    }


def fraud_eval(conn, cases: list[dict], *, use_llm: bool) -> dict:
    llm.STATS.reset()
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
        "llm": _llm_snapshot(),
    }


def _llm_snapshot() -> dict:
    st = llm.STATS
    return {
        "calls": st.attempts,
        "succeeded": st.succeeded,
        "failed": st.failed,
        "fallback_rate": st.fallback_rate,
        "reasons": dict(st.reasons),
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
    llm.STATS.reset()
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
        "llm": _llm_snapshot(),
    }


def _print_llm(snap: dict, use_llm: bool) -> None:
    """Say plainly how much of this condition was actually the model."""
    if not use_llm:
        return
    if snap["calls"] == 0:
        print("\n  model calls: none")
        return
    rate = snap["fallback_rate"]
    flag = "" if rate == 0 else ("  <- WARNING" if rate > 0.2 else "  <- some")
    print(f"\n  model calls: {snap['succeeded']}/{snap['calls']} succeeded, "
          f"{snap['failed']} fell back to rules ({rate:.0%}){flag}")
    for why, n in sorted(snap["reasons"].items(), key=lambda kv: -kv[1])[:3]:
        print(f"    {n}x {why}")
    if rate > 0.5:
        print("    NOTE: most of this condition WAS the rules path. Do not")
        print("          present it as a Nemotron result.")


def bar(label, value, width=28):
    filled = int(round(value * width))
    return f"  {label:24s} {'█' * filled}{'░' * (width - filled)} {value:.0%}"


def _evaluation_connection():
    """Conditions must not share persisted messages or holds."""
    conn = db.connect(":memory:")
    db.init(conn)
    db.seed(conn)
    return conn


def _json_results(results: dict) -> dict:
    """JSON object keys cannot be the tuple keys used by Counter."""
    out = {}
    for name, result in results.items():
        out[name] = {
            key: ({f"{truth}->{pred}": count for (truth, pred), count in value.items()}
                  if key == "confusion" else value)
            for key, value in result.items() if key != "rows"
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit machine-readable results")
    ap.add_argument("--check", action="store_true", help="fail if fraud escapes a hold or the judge is wrong")
    ap.add_argument(
        "--model",
        action="append",
        default=[],
        help="Nemotron model id to evaluate; repeat to compare several "
        "(e.g. --model nvidia/nemotron-nano-3-30b-a3b --model nvidia/nemotron-3-super-120b-a12b)",
    )
    args = ap.parse_args()

    emails = json.loads((Path(__file__).parent / "cases" / "emails.json").read_text())
    transcripts = json.loads((Path(__file__).parent / "transcripts" / "cases.json").read_text())

    live = config.have_nemotron()
    conditions: list[tuple[str, bool, str | None]] = [("rules only", False, None)]
    if live:
        models = args.model or [config.NEMOTRON_MODEL]
        conditions += [(f"rules + {m.split('/')[-1]}", True, m) for m in models]
    elif args.model:
        print("NVIDIA_API_KEY is not set, so --model has nothing to run against.\n")

    results = {}
    print("=" * 66)
    print(f"EVAL 1 — fraud detection   ({len(emails)} labeled synthetic emails)")
    print("=" * 66)
    for name, use_llm, model in conditions:
        with llm.model_override(model), closing(_evaluation_connection()) as conn:
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
        _print_llm(r["llm"], use_llm)

    print()
    print("=" * 66)
    print(f"EVAL 2 — transcript judge   ({len(transcripts)} labeled call transcripts)")
    print("=" * 66)
    for name, use_llm, model in conditions:
        with llm.model_override(model):
            j = judge_eval(transcripts, use_llm=use_llm)
        results[f"judge:{name}"] = j
        print(f"\n{name.upper()}")
        print(bar("accuracy", j["accuracy"]))
        print(f"  denial misread as confirmation: {len(j['catastrophic'])}  <- must be 0")
        wrong = [r for r in j["rows"] if r["truth"] != r["pred"]]
        for w in wrong:
            print(f"    {w['id']}: labeled {w['truth']}, judged {w['pred']}")
        _print_llm(j["llm"], use_llm)

    if not live:
        print()
        print("NOTE: NVIDIA_API_KEY is not set, so only the rules condition ran.")
        print("      Set it and re-run to produce the rules-vs-Nemotron comparison.")

    if args.json:
        Path("evals/results.json").write_text(
            json.dumps(_json_results(results), indent=2)
        )

    escaped = sum(len(result["escaped"]) for name, result in results.items() if not name.startswith("judge:"))
    judge_errors = sum(
        1 for name, result in results.items() if name.startswith("judge:")
        for row in result["rows"] if row["truth"] != row["pred"]
    )
    if args.check and (escaped or judge_errors):
        raise SystemExit(f"eval check failed: {escaped} fraud escapes, {judge_errors} judge errors")


if __name__ == "__main__":
    main()
