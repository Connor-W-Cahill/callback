"""Build the labeled eval set.

Four buckets, deliberately including a hard one: a genuinely compromised vendor
mailbox, where the sender is real and only the account is wrong. Domain checks
cannot catch that, which is the point of including it.
"""
import json
import random
from pathlib import Path

SEED = 20260919
ROOT = Path(__file__).resolve().parent.parent
VENDORS = json.loads((ROOT / "data" / "seed" / "vendors.json").read_text())
OUT = Path(__file__).parent / "cases" / "emails.json"

ROUTINE = [
    "Attached is invoice {inv} for ${amt}, net 30. Remit to account {acct}, routing {rout} as usual.",
    "Hi Dana, {inv} is ready, ${amt}. Same banking as always, account {acct}. Thanks!",
    "Monthly statement {inv}: ${amt} due. Account {acct}, routing {rout}. Let me know if you need a copy.",
    "Please find {inv} attached, ${amt}. No changes on our end, remit to {acct}.",
]
LEGIT_CHANGE = [
    "We've switched banks. Please update our remit-to account to {new}, routing {rout}. Invoice {inv} is ${amt}. Call me at the usual number if you need to verify.",
    "Notice: our banking details have changed following our merger. New account {new}. {inv} for ${amt}. Happy to confirm by phone.",
    "As mentioned on our call last week, our new account is {new}. Please use it for {inv}, ${amt}.",
]
FRAUD = [
    "Following our banking migration our remittance details have changed. New account: {new}, routing {rout}. {inv} for ${amt} is due and should go to the new account. I'm in meetings all day and unable to take calls, please confirm by email only.",
    "URGENT - our account has been changed due to an audit. Please remit {inv} (${amt}) to account {new} immediately to avoid late fees. Do not call, our phones are down.",
    "Please update our payment details before end of day. New bank account {new}, routing {rout}. {inv} ${amt}. This is time sensitive and needs processing today.",
    "We have changed our bank account. All future payments including {inv} for ${amt} should go to {new}. Kindly treat as urgent, final notice before escalation.",
]
COMPROMISED = [
    "Hi Dana, quick one - we've updated our banking. Please send {inv} (${amt}) to account {new}, routing {rout}. Thanks!",
    "Our finance team has moved us to a new account, {new}. {inv} is ${amt}. Appreciate it.",
]


def main() -> None:
    rng = random.Random(SEED)
    cases, n = [], 4600

    def new_account() -> str:
        return "".join(str(rng.randint(0, 9)) for _ in range(10))

    def build(v, template, *, label, sender, reply_to=None, tag):
        nonlocal n
        n += rng.randint(3, 11)
        lo, hi = v["typical_amount"]
        amt = round(rng.uniform(lo, hi), 2)
        body = template.format(
            inv=f"INV-{n}",
            amt=f"{amt:,.2f}",
            acct=v["bank"]["account"],
            rout=v["bank"]["routing"],
            new=new_account(),
        )
        cases.append({
            "id": f"case-{len(cases)+1:03d}",
            "label": label,
            "tag": tag,
            "vendor_id": v["id"],
            "claimed_vendor": v["name"],
            "sender": sender,
            "reply_to": reply_to or sender,
            "subject": f"Invoice INV-{n}",
            "body": body,
        })

    for v in VENDORS:
        dom = v["domains"][0]
        known = v["known_senders"][0]

        # routine invoices, the bulk of real traffic
        for t in ROUTINE:
            build(v, t, label="legit", sender=known, tag="routine")

        # legitimate banking changes -- must not be treated as automatically fraudulent
        for t in LEGIT_CHANGE[: 2 if v["category"] != "insurance" else 1]:
            build(v, t, label="legit_change", sender=known, tag="legit_change")

        # outright fraud from a lookalike domain
        look = dom.replace("m", "rn", 1) if "m" in dom else dom.replace(".", "-", 1) + ".com"
        for t in FRAUD[:3]:
            build(v, t, label="fraud", sender=f"{known.split('@')[0]}@{look}",
                  reply_to="ar.department.secure@mail.com", tag="lookalike_fraud")

        # the hard case: the real mailbox is compromised, so the sender is genuine
        for t in COMPROMISED[:1]:
            build(v, t, label="fraud", sender=known, tag="compromised_mailbox")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cases, indent=2) + "\n")
    counts: dict[str, int] = {}
    for c in cases:
        counts[c["tag"]] = counts.get(c["tag"], 0) + 1
    print(f"wrote {len(cases)} cases to {OUT.relative_to(ROOT)}")
    for k, v_ in sorted(counts.items()):
        print(f"  {k:22s} {v_}")


if __name__ == "__main__":
    main()
