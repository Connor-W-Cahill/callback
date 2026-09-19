"""Seeded inbox. Stands in for an IMAP connection we deliberately did not build."""
import json

from src import config


def load() -> list[dict]:
    return json.loads((config.SEED / "inbox.json").read_text())


def by_id(msg_id: str) -> dict:
    for m in load():
        if m["id"] == msg_id:
            return m
    raise KeyError(msg_id)
