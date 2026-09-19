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

## Running it

No API keys required. Every external call has a deterministic offline fallback,
so the pipeline runs end to end on a laptop with no network.

```bash
uv venv && uv pip install fastapi 'uvicorn[standard]' httpx python-dotenv
./run.sh            # http://localhost:8000
```

### Going live

Copy `.env.example` to `.env` and fill in the two keys:

- **`NVIDIA_API_KEY`** — sign up at [build.nvidia.com](https://build.nvidia.com), free credits on signup.
  Default model is `nvidia/nemotron-nano-3-30b-a3b` (fast; this pipeline makes three
  calls per message and demo latency matters). Swap `NEMOTRON_MODEL` to
  `nvidia/nemotron-3-super-120b-a12b` for quality.
- **`ELEVENLABS_API_KEY`** — [elevenlabs.io](https://elevenlabs.io), Profile → API Keys.
  Every SteelHacks participant gets one free month of Creator tier (131k credits).

Then verify before you demo:

```bash
python scripts/preflight.py
```

It makes one real call to each service, confirms the model exists in NVIDIA's
catalog, and prints your remaining ElevenLabs character credits and roughly how
many verification calls that buys. A broken key fails here instead of on stage.
The banner in the UI also shows which mode each service is in.

```bash
python scripts/preflight.py   # check keys + remaining credits
python evals/run_eval.py      # both evals, prints the numbers for the slide
python evals/run_eval.py --model nvidia/nemotron-nano-3-30b-a3b \
                         --model nvidia/nemotron-3-super-120b-a12b   # compare models
python evals/generate_cases.py    # regenerate the labeled email set
python data/seed/generate_history.py   # regenerate payment history
```

## Eval results

79 labeled synthetic emails, 20 labeled call transcripts. Rules-only condition
(no `NVIDIA_API_KEY` set):

| Metric | Result |
| --- | --- |
| Fraud precision | 100% (0 false positives) |
| Fraud recall | 75% (24/32) |
| Lookalike-domain fraud caught | 24/24 |
| Compromised-mailbox fraud caught *by score* | **0/8** |
| Fraud that escaped the hold entirely | **0** |
| Legitimate changes held for a call | 15 (cost: one phone call each) |
| Routine invoices wrongly held | 0 |
| Transcript judge accuracy | 100% (20/20), 0 denials misread as confirmations |

**The interesting finding.** The risk score is blind to the hard case. When a
vendor's real mailbox is compromised, the sender is genuine, the domain is
genuine, and every domain-based signal is silent — score-based detection catches
0 of 8. None of them got through anyway, because holding a payment-detail change
is *policy*, not a threshold. The score decides how loudly to shout; it never
decides whether to verify. That separation is the whole design, and the eval is
what proves it matters.

**Caveat worth stating out loud:** the transcript set is small and was written
alongside the judge's cues, so 100% there reflects an absence of hard cases more
than a strong result. Expanding it with adversarial transcripts is the obvious
next step.

## Known bug found by the eval

Case `t-15` exposed a real defect: the rules judge scanned the entire transcript,
including the agent's own script, which contains the word "confirm". The agent
was answering its own question. Fixed in `src/judge/judge.py` by restricting the
scan to `VENDOR:` turns.

## Status

Working end to end. Pipeline, API, UI, and both evals run. Not built: live IMAP,
auth, real payment rails, PDF OCR.
