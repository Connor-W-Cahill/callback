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

## Phase 4: a real phone call (optional)

With Twilio configured, an inbound email that asks to move money triggers an
actual outbound call to the vendor's number on file, records the answer, runs it
through ElevenLabs STT and Nemotron's judge, and resolves the hold — with no
human touching the browser.

It is **off by default**: all four Twilio settings must be present, live mode
(`CALLBACK_DEMO=0`) and an API password must be configured, and automatic dialing
also requires `CALLBACK_AUTO_CALL=1`. Placing real calls is the only thing this
project does with consequences outside the laptop, and an inbound email is an
untrusted trigger. The number still comes from the vendor master; nothing in an
email ever chooses who gets dialed.

### What it costs

| | |
| --- | --- |
| US local number | $1.15/month |
| Outbound, US | $0.014/min (~1¢ per 45s verification call) |
| Trial upgrade | ~$20 minimum, to remove the spoken trial notice before your audio |

Under $25 all in. The real costs are the tunnel, the setup, and one more thing
to break on stage.

### Setup

1. Create a Twilio account and buy a US local number (you must do this — it
   needs payment details).
2. Give Twilio a public URL to reach you. Locally:
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```
3. Put the values in `.env`:
   ```
   TWILIO_ACCOUNT_SID=AC...
   TWILIO_AUTH_TOKEN=...
   TWILIO_FROM=+1...
   PUBLIC_BASE_URL=https://<your-tunnel>.trycloudflare.com
   CALLBACK_AUTO_CALL=1
   CALLBACK_DEMO=0
   CALLBACK_API_PASSWORD=<choose-a-strong-password>
   ```
4. **Set a vendor's phone to a real number you control.** The seeded numbers are
   555 placeholders and are unroutable. Use the Vendors tab in demo mode to prepare
   the trusted record before restarting in live mode.

Webhooks verify Twilio's `X-Twilio-Signature` and reject anything unsigned:
`/api/twilio/recording` acts on what it receives, so an unsigned caller could
otherwise drive the fraud control. A call that goes unanswered, busy or failed
leaves the payment held and escalated — no answer is never an approval.

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

## Deploying to Vercel

```bash
npx vercel            # preview
npx vercel --prod     # production
```

Then set the keys as project environment variables — **never commit `.env`**:

```bash
npx vercel env add NVIDIA_API_KEY
npx vercel env add ELEVENLABS_API_KEY
npx vercel env add NEMOTRON_MODELS
npx vercel env add LLM_TIMEOUT
```

What the serverless port required, and what it costs:

- **State is ephemeral.** The bundle is read-only, so SQLite and recordings live
  in `/tmp`, which is per-instance and wiped on cold start. In practice every
  visitor gets their own freshly seeded demo, which is what you want when judges
  are opening the link on their own phones — but two people will not see each
  other's holds, and a hold can vanish if a later request lands on a cold
  instance. Don't leave a demo half-finished and come back to it.
- **Audio is returned inline** as a base64 data URI rather than as a URL. The
  agent's mp3 is written to one instance's `/tmp`, and the browser's follow-up
  request can land on another and 404.
- **Duration is fine.** Hobby allows 300s, and a full reset with live Nemotron
  takes ~30s.
- **HTTPS is a bonus.** `getUserMedia` needs a secure context, so the microphone
  works on the deployed URL from any phone — which localhost cannot give a judge.

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

One class of failure was **ours**, not theirs. The score job was failing about
half the time with "no JSON object in reply", and the reply text showed why: the
prompt ended with a bare `Deterministic floor score: 1.0`, never explained, and
the model spent its entire token budget wondering what that meant instead of
answering — *"That seems odd: maybe it means that if any signal fires, the score
is at least 1.0? But s—"* and then it ran out of room. Fixed twice over: that
line is gone (the floor is enforced in code and the model never needed to know),
and every job now sends a `json_schema` response format, so decoding is
constrained to valid JSON and the model cannot deliberate its way out of a reply.
That failure class went to zero.

Three mitigations for the failures that are genuinely NVIDIA's, in
`src/llm.py` and `src/config.py`:

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

## Verification safeguards and tests

Run the regression suite and deterministic evaluation without external calls:

```bash
NVIDIA_API_KEY='' ELEVENLABS_API_KEY='' CALLBACK_EMAIL=0 .venv/bin/python -m unittest discover -s tests -v
NVIDIA_API_KEY='' ELEVENLABS_API_KEY='' CALLBACK_EMAIL=0 .venv/bin/python evals/run_eval.py --check
node --check web/app.js
node --test tests/test_web.cjs
```

The evaluation now uses the production pipeline with a fixed evaluation date.
An explicit `--model` selects that exact model for every job; unavailable models
fall back to rules, not a different model. JSON results include serializable
confusion matrices. Regression tests cover contradictory extraction, routing-only
changes, uncertain replies, concurrent verification, transactional rollback,
duplicate recordings, and API access controls.

`CALLBACK_DEMO=1` is the default: browser speech, typed replies, scripted answers,
vendor editing and reset remain available for synthetic demos. To restrict the
app to actual phone verification, set `CALLBACK_DEMO=0` and a strong
`CALLBACK_API_PASSWORD`. API access then requires HTTP Basic authentication (any
username, configured password); use HTTPS for remote access. Missing credentials
fail closed. Live mode disables demo replies, reset, and vendor mutations; prepare
the trusted vendor master before switching modes. Real dialing requires live mode and a password, including automatic calls.
Signed Twilio recordings must match the attempt token and call SID stored for
the hold; the attempt token is saved before dialing so fast callbacks work. Real calls use Twilio speech so they do
not need to fetch protected audio; ElevenLabs still transcribes the response.

Settled holds cannot be reverified. An inconclusive or failed verification remains
escalated and may be retried. Verification outcomes, bank changes and their audit
entries commit together. Conflicting extracted payment details require human
review. Uploads read by the verification handler are capped at 10 MiB.

These controls do not add payment execution, multi-user roles, or durable Vercel
storage. Use persistent local storage for live-mode work; the deployed serverless
version remains a disposable synthetic demo. The queue labels cleared invoices
as ready for payment, never as paid.
