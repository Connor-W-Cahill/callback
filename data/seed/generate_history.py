"""Generate deterministic synthetic payment history for the seeded vendors.

Fixed seed on purpose: the demo must look identical every time we run it.
"""
import json
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 20260919
HERE = Path(__file__).parent
TODAY = date(2026, 9, 19)


def main() -> None:
    rng = random.Random(SEED)
    vendors = json.loads((HERE / "vendors.json").read_text())
    rows = []
    inv = 4400

    for v in vendors:
        lo, hi = v["typical_amount"]
        # Insurance bills monthly and flat; materials vendors bill irregularly.
        n = 6 if v["category"] == "insurance" else rng.randint(4, 8)
        for i in range(n):
            days_ago = 30 * (n - i) + rng.randint(-5, 5)
            amount = round(rng.uniform(lo, hi), 2) if lo != hi else lo
            inv += rng.randint(3, 19)
            rows.append(
                {
                    "invoice_number": f"INV-{inv}",
                    "vendor_id": v["id"],
                    "amount": amount,
                    "paid_to_account": v["bank"]["account"],
                    "paid_to_routing": v["bank"]["routing"],
                    "paid_on": (TODAY - timedelta(days=days_ago)).isoformat(),
                }
            )

    rows.sort(key=lambda r: r["paid_on"])
    (HERE / "payment_history.json").write_text(json.dumps(rows, indent=2) + "\n")
    print(f"wrote {len(rows)} payment history rows")


if __name__ == "__main__":
    main()
