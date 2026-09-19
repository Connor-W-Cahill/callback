"""A real inbox the demo can receive mail at.

mail.tm hands out a working address over an API with no DNS, no domain and no
credentials to manage, which is the only way to get a live inbox inside a
hackathon weekend. A judge emails the address and watches it land in the queue.

The account is persisted so the address survives a restart -- an address printed
on a slide that changes when you reboot the server is worse than no address.
"""
import json
import random
import re
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from src import config

API = "https://api.mail.tm"
ACCOUNT_FILE = config.DATA / "mailbox.json"


class MailboxError(RuntimeError):
    pass


@dataclass
class Account:
    address: str
    password: str
    token: str = ""


def _post(path: str, payload: dict, token: str = "") -> httpx.Response:
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.post(f"{API}{path}", json=payload, headers=h, timeout=25)


def _get(path: str, token: str) -> httpx.Response:
    return httpx.get(f"{API}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=25)


def _domain() -> str:
    r = httpx.get(f"{API}/domains", timeout=25)
    r.raise_for_status()
    members = r.json().get("hydra:member", [])
    if not members:
        raise MailboxError("mail.tm returned no domains")
    return members[0]["domain"]


def load_or_create() -> Account:
    """Return the persisted inbox, or provision a new one."""
    if ACCOUNT_FILE.exists():
        try:
            d = json.loads(ACCOUNT_FILE.read_text())
            acct = Account(d["address"], d["password"], d.get("token", ""))
            if authenticate(acct):
                return acct
        except Exception:  # noqa: BLE001 - fall through and provision a fresh one
            pass

    local = "".join(random.choices(string.ascii_lowercase, k=10))
    acct = Account(address=f"{local}@{_domain()}",
                   password="Hk" + "".join(random.choices(string.ascii_letters + string.digits, k=14)))
    r = _post("/accounts", {"address": acct.address, "password": acct.password})
    if r.status_code not in (200, 201):
        raise MailboxError(f"could not create inbox: {r.status_code} {r.text[:160]}")
    if not authenticate(acct):
        raise MailboxError("created the inbox but could not authenticate to it")
    return acct


def authenticate(acct: Account) -> bool:
    r = _post("/token", {"address": acct.address, "password": acct.password})
    if r.status_code != 200:
        return False
    acct.token = r.json().get("token", "")
    if acct.token:
        ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACCOUNT_FILE.write_text(json.dumps(acct.__dict__))
    return bool(acct.token)


def list_messages(acct: Account) -> list[dict]:
    r = _get("/messages", acct.token)
    if r.status_code == 401 and authenticate(acct):
        r = _get("/messages", acct.token)
    r.raise_for_status()
    return r.json().get("hydra:member", [])


def fetch(acct: Account, mail_id: str) -> dict:
    r = _get(f"/messages/{mail_id}", acct.token)
    r.raise_for_status()
    return r.json()


def to_message(mail: dict) -> dict:
    """Normalise a mail.tm message into the shape the pipeline expects."""
    frm = mail.get("from") or {}
    sender = frm.get("address", "")
    body = mail.get("text") or _strip_html(mail.get("html") or "")

    reply_to = sender
    for h, v in (mail.get("headers") or {}).items():
        if h.lower() == "reply-to":
            reply_to = _address_only(v if isinstance(v, str) else str(v))
            break

    return {
        "id": f"mail-{mail['id']}",
        "received_at": mail.get("createdAt") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sender": sender,
        "reply_to": reply_to,
        "subject": mail.get("subject", ""),
        "body": body.strip(),
        # The display name is the vendor an attacker claims to be, and is often
        # the only place the claimed identity appears.
        "claimed_vendor": frm.get("name") or None,
        "source": "email",
    }


def _strip_html(html) -> str:
    if isinstance(html, list):
        html = "\n".join(html)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"[ \t]{2,}", " ", text)


def _address_only(v: str) -> str:
    m = re.search(r"<([^>]+)>", v)
    return (m.group(1) if m else v).strip()
