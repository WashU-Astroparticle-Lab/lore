"""
Low-level Slack Web API helpers (urllib-based, no Bolt dependency).

The bot user ID is module state: ``set_bot_user_id(fetch_bot_user_id())`` is
called once at listener startup so history assembly can label the bot's own
messages as "Bot:".
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from ..config import load_env

# Populated at listener startup via set_bot_user_id().
BOT_USER_ID: str = ""


def bot_token() -> str:
    """Return SLACK_BOT_TOKEN, loading .env first. Raises KeyError if unset."""
    load_env()
    return os.environ["SLACK_BOT_TOKEN"]


def app_token() -> str:
    """Return SLACK_APP_TOKEN, loading .env first. Raises KeyError if unset."""
    load_env()
    return os.environ["SLACK_APP_TOKEN"]


def set_bot_user_id(user_id: str) -> None:
    global BOT_USER_ID
    BOT_USER_ID = user_id


def fetch_bot_user_id() -> str:
    """Fetch the bot's own user ID via auth.test (empty string on failure)."""
    req = urllib.request.Request("https://slack.com/api/auth.test")
    req.add_header("Authorization", f"Bearer {bot_token()}")
    req.add_header("User-Agent", "lab-agent/1.0")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("user_id", "")
    except Exception as exc:
        print(f"[init] Could not fetch bot user ID: {exc}")
        return ""


def slack_get(endpoint: str, params: dict[str, str]) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"https://slack.com/api/{endpoint}?{qs}")
    req.add_header("Authorization", f"Bearer {bot_token()}")
    req.add_header("User-Agent", "lab-agent/1.0")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"[history] Slack API call failed ({endpoint}): {exc}")
        return {}


def post_message(channel: str, thread_ts: str | None, text: str) -> None:
    d: dict = {"channel": channel, "text": text}
    if thread_ts:
        d["thread_ts"] = thread_ts
    payload = json.dumps(d)
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload.encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {bot_token()}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", "lab-agent/1.0")
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as exc:
        print(f"[post] Failed to post Slack message: {exc}")
