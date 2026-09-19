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

    try:
        from src import llm
        out = llm.complete_json(
            'Reply with only JSON.',
            'Return {"ok": true} and nothing else.',
            max_tokens=200,
        )
        ok(f"live call succeeded, parsed: {out}")
        return True
    except Exception as e:
        bad(f"live call failed: {e}")
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


if __name__ == "__main__":
    print("=" * 60)
    print("CALLBACK PREFLIGHT")
    print("=" * 60)
    n = check_nemotron()
    e = check_elevenlabs()
    check_voice()
    print("\n" + "=" * 60)
    if n and e:
        print("Both services live. The demo will use real Nemotron and real voice.")
    elif n or e:
        print("Partially live. The missing service falls back automatically.")
    else:
        print("Neither key set. Everything runs on deterministic fallbacks --")
        print("the demo still works end to end, just without live models.")
    print("=" * 60)
