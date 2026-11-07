"""
Conversation-history assembly for spawned Claude sessions.

How conversation history works
-------------------------------
DMs: each new top-level DM message starts a fresh conversation (no prior history).
The bot threads its reply to that message, creating a conversation thread. The user
then replies inside that thread to continue; the bot fetches the full thread history
via conversations.replies so it always sees the complete back-and-forth.
To start a new conversation: send a new top-level DM (not a thread reply).

Channel @mentions: thread-based. The bot replies inside a thread anchored to the
triggering message; subsequent replies in that thread fetch the full thread history
via conversations.replies.

Slack API search (search.messages, conversations.history on lab channels) is a
separate research tool Claude uses only to resolve vague experiment references
like "the SQUAT run from last Tuesday". It is not the same as conversation history.
"""
from __future__ import annotations

from . import api


def fetch_dm_history(channel: str, current_ts: str, limit: int = 30) -> str:
    """Return recent top-level DM messages as a formatted string (unused in normal flow).

    Kept for ad-hoc use. Normal DM sessions use fetch_history with the thread_ts.
    """
    data = api.slack_get("conversations.history", {
        "channel": channel,
        "limit": str(limit),
    })
    if not data.get("ok"):
        print(f"[history] Slack error: {data.get('error')}")
        return "(could not fetch history)"

    lines = []
    for msg in reversed(data.get("messages", [])):
        if msg.get("ts") == current_ts:
            continue
        text = api.unwrap_slack_text(msg.get("text", "")).strip()
        if not text:
            continue
        is_bot = bool(msg.get("bot_id")) or msg.get("user") == api.BOT_USER_ID
        label = "Bot" if is_bot else "User"
        lines.append(f"[{label}]: {text}")

    return "\n".join(lines) if lines else "(new conversation)"


def fetch_thread_since(channel: str, thread_ts: str, since: float, current_ts: str) -> str:
    """Other people's messages in the thread after ``since`` (epoch s), oldest first.

    For a resumed session, which remembers its own turns but not what was said in between.
    The bot's own messages are left out (it wrote them), and so is the message being
    answered. Slack returns the parent whatever ``oldest`` says, so the time is checked.
    """
    data = api.slack_get("conversations.replies", {
        "channel": channel, "ts": thread_ts, "oldest": f"{since:.6f}", "limit": "200"})
    if not data.get("ok"):
        print(f"[history] Slack error: {data.get('error')}")
        return "(could not fetch the thread)"
    lines = []
    for msg in data.get("messages", []):
        if msg.get("ts") == current_ts or float(msg.get("ts") or 0) <= since:
            continue
        if msg.get("bot_id") or msg.get("user") == api.BOT_USER_ID:
            continue
        text = api.unwrap_slack_text(msg.get("text", "")).strip()
        if text:
            lines.append(f"[<@{msg.get('user', '?')}>]: {text}")
    return "\n".join(lines)


def bot_asked_last(channel: str, thread_ts: str | None, current_ts: str) -> bool:
    """Whether the message before ``current_ts`` in the thread is the bot asking something.

    Then a short "ok" or "sure" is an answer, not an acknowledgement to ignore. When the
    thread cannot be read, True: answering an "ok" beats dropping a yes.
    """
    if not thread_ts:
        return False
    data = api.slack_get("conversations.replies", {
        "channel": channel, "ts": thread_ts, "latest": current_ts, "inclusive": "false",
        "limit": "200"})
    if not data.get("ok"):
        return True
    earlier = [m for m in data.get("messages", [])
               if float(m.get("ts") or 0) < float(current_ts)]
    if not earlier:
        return False
    last = max(earlier, key=lambda m: float(m.get("ts") or 0))
    is_bot = bool(last.get("bot_id")) or last.get("user") == api.BOT_USER_ID
    return is_bot and "?" in api.unwrap_slack_text(last.get("text", ""))[-400:]


def fetch_history(channel: str, thread_ts: str | None, current_ts: str) -> str:
    """Return thread history for channel @mentions via conversations.replies.

    If thread_ts is None this is a new top-level mention — no prior context.
    """
    if thread_ts is None:
        return "(new conversation)"

    data = api.slack_get("conversations.replies", {
        "channel": channel,
        "ts": thread_ts,
        "limit": "200",
    })

    if not data.get("ok"):
        print(f"[history] Slack error: {data.get('error')}")
        return "(could not fetch history)"

    lines = []
    for msg in data.get("messages", []):
        if msg.get("ts") == current_ts:
            continue
        text = api.unwrap_slack_text(msg.get("text", "")).strip()
        if not text:
            continue
        is_bot = bool(msg.get("bot_id")) or msg.get("user") == api.BOT_USER_ID
        label = "Bot" if is_bot else "User"
        lines.append(f"[{label}]: {text}")

    return "\n".join(lines) if lines else "(new conversation)"
