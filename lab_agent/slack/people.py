"""Who said what: Slack user IDs turned into the names sessions can use.

A session used to be told only the sender's Slack ID, and every earlier message in the
thread was "[User]". A lab member asked LORE to look something up for a colleague, and
LORE's reply suggested asking someone, by name, who turned out to be the person asking. It
could not have known who that was.

Names come from ``users.info`` (the bot's users:read scope), cached in memory and in
``.lore_tmp/slack_names.json`` for a month, so a name costs one call per person. Without
the scope, or offline, an ID stays an ID: nothing here can stop a session.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..config import PROJECT_ROOT
from . import api

CACHE_PATH = PROJECT_ROOT / ".lore_tmp" / "slack_names.json"
CACHE_DAYS = 30
_MENTION = re.compile(r"<@([UW][A-Z0-9]{2,})(?:\|[^>]*)?>")

_memory: dict[str, str | None] = {}


def _lookup(user_id: str) -> str | None:
    """The person's real name from Slack, or None (a bot, no scope, offline)."""
    try:
        user = api.api_get("users.info", {"user": user_id}).get("user") or {}
    except Exception:  # noqa: BLE001 — missing_scope, network: the ID stays an ID
        return None
    if user.get("is_bot"):
        return None
    profile = user.get("profile") or {}
    return (user.get("real_name") or profile.get("real_name") or profile.get("display_name")
            or user.get("name") or None)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def name_of(user_id: str | None, cache_path: Path | None = None) -> str | None:
    """``user_id``'s name, or None when it cannot be found."""
    if not user_id:
        return None
    if user_id in _memory:
        return _memory[user_id]
    path = cache_path or CACHE_PATH
    disk = _load(path)
    entry = disk.get(user_id)
    if entry and time.time() - entry.get("at", 0) < CACHE_DAYS * 86400:
        _memory[user_id] = entry["name"]
        return entry["name"]
    name = _lookup(user_id)
    _memory[user_id] = name
    if name:
        disk[user_id] = {"name": name, "at": time.time()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(disk, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
    return name


def label(user_id: str | None, bot: bool = False) -> str:
    """How a message's author is shown to a session: "Ada Lovelace (<@U0123ABCD>)"."""
    if bot or (user_id and user_id == api.BOT_USER_ID):
        return "LORE (you)"
    name = name_of(user_id)
    return f"{name} (<@{user_id}>)" if name else f"<@{user_id or '?'}>"


def named(text: str) -> str:
    """``text`` with each ``<@U…>`` mention followed by the person's name, so a session
    reading "<@U0123ABCD> can you check" knows who is meant. The mention is kept: it
    is what a reply must use to tag someone."""
    def sub(m: re.Match) -> str:
        uid = m.group(1)
        if uid == api.BOT_USER_ID:
            return f"<@{uid}> (LORE, you)"
        name = name_of(uid)
        return f"<@{uid}> ({name})" if name else m.group(0)
    return _MENTION.sub(sub, text or "")


def first_name(user_id: str | None) -> str | None:
    name = name_of(user_id)
    return name.split()[0] if name else None
