# Demo runbook

Ninety seconds. Told as a story about a person, not a tour of a dashboard.

## Before you walk up

```bash
python scripts/preflight.py        # keys live? credits left?
./run.sh                           # http://localhost:8000
```

Then, in the browser:

1. Click **Reset demo**. It takes ~30s with live Nemotron — **do this before you
   are standing in front of anyone**, never during.
2. Confirm the banner top-right reads `nemotron: live · voice: live`.
3. Confirm two holds appear: Allegheny (high risk) and Keystone (review).
4. Click Allegheny, click **Place verification call**, and confirm you hear
   audio. Then Reset again so the call is unused.
5. Plug in headphones or test the room speakers. A demo nobody can hear is a
   demo that did not happen.
6. Close every other tab. Zoom the browser to ~125% so judges can read it.

## The run of show

**Open (10s)** — say who you are, not what you built.
> "I'm Dana. I do accounts payable for a forty-person HVAC contractor in
> Pittsburgh. I pay about sixty invoices a month and nobody has ever trained
> me on fraud."

**The happy path (15s)** — point at the *Cleared* list.
> "Two invoices came in this morning from vendors we know. They cleared and got
> paid. You'll notice the tool did nothing, which is the point — it has to be
> invisible when nothing is wrong."

**The attack (20s)** — open the Allegheny hold, read the email aloud from the
panel. Read it straight, do not editorialise.
> "Following our banking migration our remittance details have changed... I'm in
> meetings all day and unable to take calls, so please just confirm by email."

Then pause. Let the room notice it looks completely ordinary.
> "That is what this attack looks like. No malware, no bad link. Just a familiar
> name asking for a routine change."

**The catch (15s)** — point at the fired signals, read two or three.
> "The remit-to account differs from all four prior payments. The sender domain
> is `alleghenysheetrnetal.com` — r-n, not m. Reply-to points somewhere else
> entirely. And it discourages calling, which we treat as a signal in itself."

**The call (25s)** — the climax. Click **Place verification call**.
> "The email is the channel that's compromised. So we don't use email to check
> it. We call the number already on file."

Let the audio play. **Do not talk over it.**

Then hand a judge the mic and click record:
> "You're the vendor. Did you send that request?"

They say no. Stop recording. The transcript appears, judged `denied`, payment
**blocked** — on their own voice.

**The close (10s)**
> "The email was the attack. So we didn't use email to check it. Nemotron
> extracts, scores and judges; ElevenLabs makes the call real. And here's the
> part we didn't expect—"

Show the eval slide: score-based detection catches **0 of 8** compromised-mailbox
attacks, and **none escaped** — because holding a payment-detail change is
policy, not a threshold.

> "The score decides how loudly to shout. It never decides whether to verify."

## The mic moment

This is the thing judges will remember, so protect it:

- **Ask permission first.** "Can I hand you the mic for a second?"
- Give them the line to say. Judges freeze when handed an open prompt.
  *"Just say: no, we never sent that."*
- Have a teammate ready to say it if the judge declines.
- Grant microphone permission in the browser **before** you present. Chrome's
  permission prompt mid-demo is a ten-second stall.

## When it breaks

| If | Do |
|---|---|
| Banner says `nemotron: offline` | Say so plainly: "the model is down, it's running on the deterministic path." It still works. Do not pretend. |
| Voice is `simulated` | Use **Use the scripted reply**. Same transcript, same judgment. |
| Mic is blocked or the room is loud | Type the reply instead. The judge cannot tell from the transcript. |
| A call hangs >15s | Click **Use the scripted reply** and keep talking. Never watch a spinner in silence. |
| Everything is on fire | Reset, and demo the already-blocked Allegheny hold. The transcript and verdict are persisted from the last run. |

**Never apologise for the fallbacks.** Degrading gracefully is a design decision
and a selling point: "the free tier goes down constantly, so a payment control
that depends on a model being up isn't a payment control."

## Questions you will get

**"Wouldn't a bank just build this?"**
Banks see the payment, not the email that caused it. The change happens in the
customer's AP system before anything reaches the bank.

**"How does it actually decide?"**
Ten deterministic features, computed in Python, listed on screen. The model
weighs and explains them — it never invents evidence, and it can raise the
score but never lower it.

**"What about false positives?"**
Blocking a real vendor change costs a two-minute phone call. Missing a fraudulent
one costs the invoice. The threshold is loose on purpose. Zero routine invoices
were wrongly held across 79 labeled cases.

**"Isn't 100% suspicious?"**
Yes, and say so first. It's synthetic data we generated. The transcript judge
scoring 20/20 reflects a small set written alongside its own cues more than a
strong result. The honest number is the 0/8.

## Do not

- Do not reset while judges watch.
- Do not read the code out loud.
- Do not say "we ran out of time." Say what it does today.
- Do not claim Nemotron results without checking the fallback line in the eval
  output. If most calls fell back, that column is the rules path.
