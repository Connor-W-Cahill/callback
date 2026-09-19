# Callback

**Vendor payment-change fraud interceptor.** Built at SteelHacks XIII, University of Pittsburgh, Sept 19–20 2026.

In a business email compromise attack, someone emails accounts payable posing as a vendor you already
pay and says their banking details have changed. AP updates the record, the next invoice gets paid, and
the money goes to the attacker. The defense is callback verification: phone the vendor at the number
already on file and confirm. It works, and it is the first thing skipped when AP is busy.

Callback does it automatically. It reads inbound vendor email, scores how likely a payment-detail change
is fraudulent, holds the payment, places the verification call itself, and judges what the vendor said.

**The email is the attack, so we don't use email to check it.**

## How it works

1. **Ingest** — inbound invoice/vendor email lands in the AP mailbox
2. **Extract** — structured fields out of unstructured text (vendor, account, routing, amount, reply-to)
3. **Resolve** — fuzzy-match the claimed vendor against the vendor master
4. **Score** — risk features vs. vendor history, plus a written rationale naming what fired
5. **Hold** — anything touching payment details is held; this is policy, not a score
6. **Call back** — voice agent dials the *on-file* number and asks the vendor to confirm
7. **Judge** — a model reads the transcript: confirmed / denied / unclear (unclear escalates to a human)
8. **Resolve** — write to the vendor master with an audit entry, or stay blocked and flag

## Risk signals

| Signal | Why |
| --- | --- |
| Account differs from last N payments | The hardest signal. Legit vendors rarely switch banks. |
| Sender domain is a near-miss | Typosquatting: `acme-corp.com` vs `acmecorp.com` |
| Reply-to ≠ From | Classic redirect so replies reach the attacker |
| First contact from this address | Two years of the same three addresses, then a new one |
| Urgency / discourages calling | "Please don't call, I'm in meetings" — pre-empting the callback |
| Amount anomaly | Outside this vendor's range, or just under an approval threshold |
| Timed near a payment run | Landing right before money moves is not a coincidence |
| Bank country/routing off-profile | Domestic supplier suddenly remitting overseas |

False positives cost a two-minute phone call. False negatives cost the invoice. The threshold is loose on purpose.

## Tracks

- **Compound** — payments, fraud, small-business cash flow
- **Beyond the Chatbot (Nemotron)** — extraction, classification, and model-as-judge in a pipeline; see `evals/`
- **Out Loud (ElevenLabs)** — the callback *is* the control; a text box cannot substitute for it
- **Seed Round** — real category, real buyers

## Layout

```
src/ingest/    seeded inbox, message parsing
src/extract/   Nemotron job 1 — text to structured JSON
src/score/     risk features + scoring + rationale
src/voice/     ElevenLabs outbound call, TTS/STT
src/judge/     Nemotron job 3 — transcript to confirmed/denied/unclear
src/api/       backend for the hold queue
web/           AP clerk hold queue UI
data/seed/     synthetic vendors + invoice history
evals/cases/   labeled synthetic emails
evals/transcripts/  labeled synthetic call transcripts
```

## Data

All synthetic. No real account numbers, credentials, or financial records — required by the Compound
track rules and a good idea regardless.

## Status

Scaffold. See the planning doc for scope, build order, and the demo script.
