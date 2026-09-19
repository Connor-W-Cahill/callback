"""Preflight: are the keys live, and how much credit is left?

Run this before the demo. It makes one cheap real call to each service so a
broken key fails here rather than on stage.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from src import config


def ok(msg):
    print(f"  \033[32m✓\033[0m {msg}")


def bad(msg):
    print(f"  \033[31m✗\033[0m {msg}")


def check_nemotron() -> bool:
    print("\nNEMOTRON")
    if not config.have_nemotron():
        bad("NVIDIA_API_KEY not set -- pipeline will use the rules fallback")
        return False
    print(f"  model: {config.NEMOTRON_MODEL}")
    try:
        r = httpx.get(f"{config.NVIDIA_BASE_URL}/models", timeout=20)
        ids = [m["id"] for m in r.json().get("data", [])]
        if config.NEMOTRON_MODEL in ids:
            ok(f"model exists in the catalog ({len(ids)} available)")
        else:
            bad(f"model NOT in catalog. Nemotron options: "
                f"{', '.join(i for i in ids if 'nemotron' in i)[:200]}")
            return False
    except Exception as e:
        bad(f"could not read model catalog: {e}")
        ids = []

    from src import llm

    def try_model(model: str) -> bool:
        config.NEMOTRON_MODEL = model
        try:
            out = llm.complete_json(
                "Reply with only JSON.",
                'Return {"ok": true} and nothing else.',
                max_tokens=200,
            )
            ok(f"live call succeeded with {model}, parsed: {out}")
            return True
        except Exception as e:
            bad(f"{model} failed: {str(e)[:110]}")
            return False

    if try_model(config.NEMOTRON_MODEL):
        return True

    # Catalog membership does not imply access: most ids 404 per-account.
    print("  trying other nemotron models on the catalog...")
    for m in [i for i in ids if "nemotron" in i and "embed" not in i
              and "parse" not in i and "safety" not in i and "reward" not in i][:6]:
        if m == config.NEMOTRON_MODEL:
            continue
        if try_model(m):
            print(f"\n  \033[33m→ set NEMOTRON_MODEL={m} in your .env\033[0m")
            return True
    return False


def check_elevenlabs() -> bool:
    print("\nELEVENLABS")
    if not config.have_elevenlabs():
        bad("ELEVENLABS_API_KEY not set -- calls will be simulated")
        return False
    try:
        r = httpx.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": config.ELEVENLABS_API_KEY},
            timeout=20,
        )
        r.raise_for_status()
        d = r.json()
        used = d.get("character_count", 0)
        limit = d.get("character_limit", 0)
        left = limit - used
        ok(f"key valid, tier: {d.get('tier', '?')}")
        ok(f"credits: {left:,} of {limit:,} characters remaining ({used:,} used)")
        if left < 5000:
            bad(f"only {left:,} characters left -- roughly "
                f"{left // 320} more verification calls. Watch this.")
        return True
    except Exception as e:
        bad(f"subscription check failed: {e}")
        return False


def check_voice() -> None:
    if not config.have_elevenlabs():
        return
    try:
        r = httpx.get(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": config.ELEVENLABS_API_KEY},
            timeout=20,
        )
        voices = r.json().get("voices", [])
        match = [v for v in voices if v["voice_id"] == config.ELEVENLABS_VOICE_ID]
        if match:
            ok(f"voice '{match[0]['name']}' ({config.ELEVENLABS_VOICE_ID}) available")
        else:
            bad(f"voice {config.ELEVENLABS_VOICE_ID} not in your {len(voices)} voices. "
                f"Try: {', '.join(v['name'] + '=' + v['voice_id'] for v in voices[:3])}")
    except Exception as e:
        bad(f"voice list failed: {e}")


def check_twilio() -> bool:
    print("\nTWILIO (phase 4 — real phone calls)")
    from src.voice import telephony

    missing = [
        n for n, v in (
            ("TWILIO_ACCOUNT_SID", config.TWILIO_SID),
            ("TWILIO_AUTH_TOKEN", config.TWILIO_TOKEN),
            ("TWILIO_FROM", config.TWILIO_FROM),
            ("PUBLIC_BASE_URL", config.PUBLIC_BASE_URL),
        ) if not v
    ]
    if missing:
        bad(f"not configured — missing {', '.join(missing)}")
        print("     (the browser-based callback still works; this is optional)")
        return False

    try:
        r = httpx.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_SID}.json",
            auth=(config.TWILIO_SID, config.TWILIO_TOKEN), timeout=20,
        )
        if r.status_code != 200:
            bad(f"credentials rejected: {r.status_code} {r.text[:120]}")
            return False
        acct = r.json()
        ok(f"account '{acct.get('friendly_name')}' — type: {acct.get('type')}, status: {acct.get('status')}")
        if acct.get("type") == "Trial":
            bad("TRIAL account: Twilio speaks a trial notice BEFORE your audio, and")
            print("     you can only call numbers you have verified. Upgrade before demoing.")
    except Exception as e:
        bad(f"could not reach Twilio: {e}")
        return False

    # Does the From number actually belong to this account?
    try:
        r = httpx.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_SID}/IncomingPhoneNumbers.json",
            auth=(config.TWILIO_SID, config.TWILIO_TOKEN), timeout=20,
        )
        nums = [n["phone_number"] for n in r.json().get("incoming_phone_numbers", [])]
        if config.TWILIO_FROM in nums:
            ok(f"caller id {config.TWILIO_FROM} is owned by this account")
        else:
            bad(f"TWILIO_FROM {config.TWILIO_FROM} is not on this account. Owned: {nums or 'none — buy a number'}")
            return False
    except Exception as e:
        bad(f"could not list numbers: {e}")

    # Twilio must be able to reach us, or the recording never comes back.
    try:
        u = config.PUBLIC_BASE_URL + "/api/status"
        r = httpx.get(u, timeout=15)
        if r.status_code == 200:
            ok(f"Twilio can reach {config.PUBLIC_BASE_URL} (tunnel is up)")
        else:
            bad(f"{u} returned {r.status_code} — Twilio will not be able to post recordings back")
    except Exception as e:
        bad(f"PUBLIC_BASE_URL unreachable: {str(e)[:90]}")
        print("     Start a tunnel, then set PUBLIC_BASE_URL to the https URL it prints.")
        return False

    if not config.AUTO_CALL:
        print("  \033[33m!\033[0m configured, but CALLBACK_AUTO_CALL=0 — inbound email will NOT auto-dial.")
        print("     Set CALLBACK_AUTO_CALL=1 to arm it.")
    else:
        ok("auto-call ARMED: held inbound email will place a real call")

    # The seeded 555 numbers are unroutable; auto-dial would silently do nothing.
    try:
        from src import db
        c = db.connect(); db.init(c)
        reals = [v for v in db.vendors(c) if not v["phone_on_file"].replace("-", "").replace(" ", "").startswith("+1412555")
                 and "555" not in v["phone_on_file"]]
        if reals:
            ok(f"{len(reals)} vendor(s) have a non-placeholder phone: "
               f"{', '.join(v['name'] + ' ' + v['phone_on_file'] for v in reals[:3])}")
        else:
            bad("every vendor still has a 555 placeholder number — auto-dial will reach nobody.")
            print("     Set a real number you control on a vendor in the Vendors tab.")
    except Exception:
        pass
    return True


if __name__ == "__main__":
    print("=" * 60)
    print("CALLBACK PREFLIGHT")
    print("=" * 60)
    n = check_nemotron()
    e = check_elevenlabs()
    check_voice()
    check_twilio()
    print("\n" + "=" * 60)
    if n and e:
        print("Both services live. The demo will use real Nemotron and real voice.")
    elif n or e:
        print("Partially live. The missing service falls back automatically.")
    else:
        print("No service is live. Everything runs on deterministic fallbacks --")
        print("the demo still works end to end, just without live models.")
    print("=" * 60)
