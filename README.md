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
6. **Call back** — voice agent dials the *on-file* number and asks the vendor to confirm.
   The vendor's answer arrives one of three ways, all producing the same transcript:
   **spoken** (a human talks, ElevenLabs STT transcribes), **typed**, or **scripted**
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

## The verification call

Click a held payment, then **Place verification call**. The agent's question is
rendered by ElevenLabs TTS and plays in the browser. Then the vendor answers:

- **Speak it.** Click record, talk into the mic, stop. The audio goes to
  ElevenLabs speech-to-text and the transcript goes to the judge. Nothing about
  the reply was written in advance — hand a judge the mic and let them say
  "we never sent that."
- **Type it.** Same path, no key needed.
- **Use the scripted reply.** Deterministic, for a demo run that must not vary.

The transcript shape is identical either way, so the judge cannot tell how the
words were captured. Both audio clips are kept and replayable in the UI.

## Under the hood

The second tab in the UI shows **every call to Nemotron and ElevenLabs as it
happened** — which job, which model, how long it took, what was sent and what came
back, and which calls failed and fell through to the next model.

It exists because both tool tracks ask us to prove something rather than assert it:
Nemotron wants to see where the model fits and evidence it works, ElevenLabs wants
voice to be a real part of how the project works. A judge can watch the requests
land instead of taking our word for it. It also makes the fallback behaviour
visible: when a model times out you can see exactly that, rather than wondering
why a rationale reads a bit flat.

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
  **Being listed in NVIDIA's public model catalog does not mean your account can
  call it** — most `nemotron` ids return 404 "not found for account". Run the
  preflight, which tries the configured model and auto-discovers a working
  substitute if it fails.
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

## Notes from running it live

**NVIDIA's free tier is unreliable, and you should plan around it.** Measured
over one afternoon: `nemotron-nano-3-30b-a3b` and most `llama-*-nemotron` ids
return `404 not found for account` despite being in the public catalog;
`nemotron-3.5-lightning-30b-a3b` served fine one hour and timed out every call
the next; `nemotron-3-super-120b-a12b` went from 0.9s to 503 to timeout within
an hour. Identical requests return clean JSON, truncated bodies, or 503s.

Three mitigations, all in `src/llm.py` and `src/config.py`:

- **A model chain** (`NEMOTRON_MODELS`), tried in order, then rules. One dead
  model costs one timeout, not the request.
- **A short timeout** (`LLM_TIMEOUT`, default 12s). A healthy call returns in
  5–8s; there is no value in waiting two minutes to learn it will not.
- **Concurrent processing.** `/api/reset` runs the inbox through a thread pool,
  turning a minute of dead air before a demo into ~30s.

Critically, the eval **reports how many model calls fell back** — without that
number a fallback is indistinguishable from a model that agreed with the rules,
and a "rules + Nemotron" column could quietly be rules. Check that line before
quoting any comparison to a judge.

### ElevenLabs, verified live

TTS and STT both round-trip correctly: the agent's question renders, real audio
transcribes back verbatim, and the judge acts on words nobody scripted. Note the
free tier is **10,000 characters (~31 verification calls)**, not the 131k that
the SteelHacks Creator-tier perk grants — redeem that separately.

## Known bug found by the eval

Case `t-15` exposed a real defect: the rules judge scanned the entire transcript,
including the agent's own script, which contains the word "confirm". The agent
was answering its own question. Fixed in `src/judge/judge.py` by restricting the
scan to `VENDOR:` turns.

## Status

Working end to end. Pipeline, API, UI, and both evals run. Not built: live IMAP,
auth, real payment rails, PDF OCR.
