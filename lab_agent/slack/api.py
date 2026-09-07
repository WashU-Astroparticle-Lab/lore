"""
Low-level Slack Web API helpers (urllib-based, no Bolt dependency).

The bot user ID is module state: ``set_bot_user_id(fetch_bot_user_id())`` is
called once at listener startup so history assembly can label the bot's own
messages as "Bot:".
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

from ..config import load_env

# Populated at listener startup via set_bot_user_id().
BOT_USER_ID: str = ""


# Slack auto-links URLs in a message's `text` as <url> or <url|label>. Match only
# real links (http/https/mailto) so user/channel mentions (<@U…>, <#C…|name>,
# <!here>) are left untouched.
_SLACK_LINK_RE = re.compile(r"<(?P<url>(?:https?://|mailto:)[^|>\s]+)(?:\|[^>]*)?>")


def unwrap_slack_text(text: str) -> str:
    """Undo Slack's message formatting so downstream consumers see plain text.

    Slack wraps links in a message's ``text`` as ``<url>`` or ``<url|label>`` and
    HTML-escapes ``&``, ``<``, ``>``. Left as-is, a GitHub URL reaches the spawned
    agent as ``<https://github.com/...>`` and the leading ``<`` breaks run.py's URL
    parsing (the arg no longer starts with "http"). This replaces each auto-linked
    URL with its bare URL and unescapes entities. Mentions are left untouched.
    """
    if not text:
        return text
    text = _SLACK_LINK_RE.sub(lambda m: m.group("url"), text)
    # Unescape entities; &amp; last so an escaped "&lt;" (sent as "&amp;lt;")
    # round-trips correctly rather than collapsing to "<".
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


# A message carrying only an uploaded file arrives with subtype "file_share" and
# usually empty text. The DM handler used to drop every subtype, so those never
# woke the bot — after "just drop it in the thread and I'll kick off the
# pipeline", the user uploaded a photo 51 s later and waited six minutes before
# nudging. Exactly one subtype is let through.
ALLOWED_MESSAGE_SUBTYPES = frozenset({"file_share"})


def dm_event_text(event: dict) -> str | None:
    """Text to act on for a DM event, or None if the event should be ignored.

    Returns unwrapped text with an ``[uploaded file(s): …]`` note appended when
    the message carries files, so a text-less upload is still something the
    session can act on rather than an empty string.
    """
    subtype = event.get("subtype")
    if event.get("bot_id"):
        return None
    if subtype and subtype not in ALLOWED_MESSAGE_SUBTYPES:
        return None

    text = unwrap_slack_text(event.get("text") or "").strip()
    names = [f.get("name", "file") for f in (event.get("files") or [])]
    if names:
        # Carry the FILE MESSAGE's own ts, not just the names. The spawn prompt
        # otherwise offers only the thread_ts, and a session reaching for the
        # attachments naturally tries that first — which returns nothing, because
        # the files hang off this message, not the thread parent. Observed live:
        # one wasted fetch-files call plus a read-thread round-trip to recover.
        ts = event.get("ts", "")
        where = f" — message ts {ts}" if ts else ""
        text = (text + f"\n[uploaded file(s): {', '.join(names)}{where}]").strip()
    return text or None


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


class SlackError(RuntimeError):
    """A Slack API call failed.

    Slack answers a *rejected* call with HTTP 200 and ``{"ok": false, ...}``, so
    "no exception" is not evidence that anything happened. Every checked helper
    below raises this instead of returning quietly.
    """


def api_post(endpoint: str, payload: dict, timeout: int = 30) -> dict:
    """POST JSON to a Slack Web API method. Raises SlackError unless ok:true."""
    req = urllib.request.Request(
        f"https://slack.com/api/{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {bot_token()}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", "lab-agent/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except Exception as exc:
        raise SlackError(f"{endpoint}: transport error: {exc}") from exc
    if not body.get("ok"):
        detail = body.get("error", "unknown_error")
        needed = body.get("needed") or body.get("response_metadata", {}).get("messages")
        raise SlackError(f"{endpoint}: {detail}" + (f" (needed: {needed})" if needed else ""))
    return body


def api_get(endpoint: str, params: dict, timeout: int = 30) -> dict:
    """GET a Slack Web API method. Raises SlackError unless ok:true."""
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"https://slack.com/api/{endpoint}?{qs}")
    req.add_header("Authorization", f"Bearer {bot_token()}")
    req.add_header("User-Agent", "lab-agent/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except Exception as exc:
        raise SlackError(f"{endpoint}: transport error: {exc}") from exc
    if not body.get("ok"):
        detail = body.get("error", "unknown_error")
        if body.get("needed"):
            # missing_scope is the most common failure and is trivially fixable —
            # name the scope instead of making the caller go and look it up.
            detail += f" (needs scope: {body['needed']}; token has: {body.get('provided', '?')})"
        raise SlackError(f"{endpoint}: {detail}")
    return body


def post_message_checked(channel: str, thread_ts: str | None, text: str) -> str:
    """Post a message and return its ts. Raises SlackError if it did not post."""
    d: dict = {"channel": channel, "text": text}
    if thread_ts:
        d["thread_ts"] = thread_ts
    body = api_post("chat.postMessage", d)
    ts = body.get("ts", "")
    if not ts:
        raise SlackError("chat.postMessage: ok but no ts returned")
    return ts


def message_exists(channel: str, ts: str) -> bool:
    """Read a message back by ts — proof it is actually in the conversation."""
    try:
        body = api_get("conversations.history", {
            "channel": channel, "latest": ts, "oldest": ts,
            "inclusive": "true", "limit": "1",
        })
    except SlackError:
        return False
    return any(m.get("ts") == ts for m in body.get("messages", []))


def upload_file(
    channel: str,
    thread_ts: str | None,
    path,
    title: str | None = None,
    comment: str | None = None,
) -> str:
    """Upload a file to a channel/thread and return its file id.

    Implements Slack's three-step external upload (``files.upload`` is
    deprecated and answers ``method_deprecated``). Every step is checked, so a
    partial upload raises instead of being reported as success — the failure
    mode this replaces was announcing "both plots are in your thread now" when
    nothing had been shared.
    """
    from pathlib import Path as _Path

    p = _Path(path)
    data = p.read_bytes()
    if not data:
        raise SlackError(f"{p.name}: file is empty, nothing to upload")

    # 1. reserve an upload URL
    body = api_get("files.getUploadURLExternal", {"filename": p.name, "length": str(len(data))})
    upload_url, file_id = body.get("upload_url", ""), body.get("file_id", "")
    if not upload_url or not file_id:
        raise SlackError("files.getUploadURLExternal: ok but no upload_url/file_id")

    # 2. PUT the bytes
    put = urllib.request.Request(upload_url, data=data, method="POST")
    put.add_header("Content-Type", "application/octet-stream")
    put.add_header("User-Agent", "lab-agent/1.0")
    try:
        with urllib.request.urlopen(put, timeout=120) as resp:
            if resp.status not in (200, 201):
                raise SlackError(f"{p.name}: upload PUT returned HTTP {resp.status}")
    except SlackError:
        raise
    except Exception as exc:
        raise SlackError(f"{p.name}: upload PUT failed: {exc}") from exc

    # 3. complete, attaching it to the conversation
    payload: dict = {
        "files": [{"id": file_id, "title": title or p.name}],
        "channel_id": channel,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if comment:
        payload["initial_comment"] = comment
    api_post("files.completeUploadExternal", payload)
    return file_id


def file_is_shared(file_id: str, channel: str, attempts: int = 5, delay: float = 0.8) -> bool:
    """Read an uploaded file back and confirm it reached *channel*.

    Retries, because ``files.completeUploadExternal`` returns before Slack has
    indexed the share: a single immediate check reported "uploaded but not
    visible" for three files that were already sitting in the thread. A
    verification that fails closed on a race is worse than none — it made the
    agent announce a permission problem that did not exist and invent an
    explanation for it.
    """
    import time as _time

    for attempt in range(attempts):
        try:
            f = api_get("files.info", {"file": file_id}).get("file", {})
        except SlackError:
            f = {}
        shared = set(f.get("channels") or []) | set(f.get("groups") or []) | set(f.get("ims") or [])
        for entries in (f.get("shares") or {}).values():
            shared |= set(entries.keys())
        if channel in shared:
            return True
        if attempt < attempts - 1:
            _time.sleep(delay)
    return False


def list_channels(limit: int = 200) -> tuple[list[dict], str | None]:
    """Channels the bot can post to. Returns (channels, warning).

    Listing private channels needs ``groups:read``; listing public ones needs
    ``channels:read``. Slack rejects the WHOLE call with ``missing_scope`` if any
    requested type is not granted — so asking for both at once turns a missing
    private-channel scope into "no channels at all". Fall back to public-only and
    say what is missing, rather than returning nothing.
    """
    params = {"exclude_archived": "true", "limit": str(limit)}
    warning = None
    try:
        body = api_get("conversations.list", {**params, "types": "public_channel,private_channel"})
    except SlackError as exc:
        if "missing_scope" not in str(exc):
            raise
        body = api_get("conversations.list", {**params, "types": "public_channel"})
        warning = ("private channels are not listed — the bot token lacks 'groups:read'. "
                   "Public channels below are complete; name a private channel explicitly to post to it.")
    channels = [
        {"id": c["id"], "name": c.get("name", ""), "is_member": bool(c.get("is_member")),
         "is_private": bool(c.get("is_private"))}
        for c in body.get("channels", [])
    ]
    return channels, warning


def search_history(query: str, limit_per_channel: int = 200, max_channels: int = 25) -> list[dict]:
    """Case-insensitive substring search across channels the bot is a member of.

    Slack's ``search.messages`` needs a USER token (`not_allowed_token_type` on a
    bot token), so the workspace-wide search that CLAUDE.md documented could never
    run. This does the next best thing with scopes the bot actually has: pull
    recent history from each member channel and match locally.

    Returns newest-first dicts: channel, ts, user, text, permalink-ish location.
    """
    needle = query.lower().strip()
    if not needle:
        return []
    channels, _ = list_channels()
    hits: list[dict] = []

    def _brief(m: dict) -> dict:
        return {
            "user": m.get("user", "?"),
            "text": " ".join(unwrap_slack_text(m.get("text", "")).split())[:160],
            "files": [f.get("name", "file") for f in (m.get("files") or [])],
        }

    for ch in [c for c in channels if c["is_member"]][:max_channels]:
        try:
            body = api_get("conversations.history",
                           {"channel": ch["id"], "limit": str(limit_per_channel)})
        except SlackError:
            continue  # one unreadable channel must not sink the search
        messages = body.get("messages", [])          # newest-first
        for idx, m in enumerate(messages):
            text = unwrap_slack_text(m.get("text", ""))
            if needle in text.lower():
                # Neighbours matter: a claim and the screenshots backing it are
                # routinely separate messages, so a match shown alone reads as a
                # bare assertion and invites concluding more than it supports.
                neighbours = [_brief(messages[j]) for j in (idx + 1, idx - 1)
                              if 0 <= j < len(messages)]
                hits.append({
                    "channel": ch["name"], "channel_id": ch["id"],
                    "ts": m.get("ts", ""), "user": m.get("user", "?"), "text": text,
                    # A message's meaning often lives in its attachments.
                    "files": [f.get("name", "file") for f in (m.get("files") or [])],
                    "nearby": neighbours,
                })
    hits.sort(key=lambda h: h["ts"], reverse=True)
    return hits


def fetch_message_files(channel: str, ts: str, out_dir) -> list:
    """Download the files attached to one message. Returns local paths.

    Knowing that a message *has* images is not the same as knowing what they
    show — a claim in the text can be contradicted or narrowed by its own
    screenshots. This pulls them to disk so an agent can actually look.

    Slack's ``url_private`` needs the bot token as a bearer header; fetching it
    unauthenticated returns an HTML login page, not the image.
    """
    from pathlib import Path as _Path

    out = _Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # conversations.history only returns TOP-LEVEL messages, so a file posted as a
    # thread reply is invisible to it — and replies are exactly where figures get
    # attached. Fall back to conversations.replies, which resolves a reply's own ts.
    messages = api_get("conversations.history", {
        "channel": channel, "latest": ts, "oldest": ts, "inclusive": "true", "limit": "1",
    }).get("messages", [])
    if not messages:
        messages = [m for m in api_get("conversations.replies", {
            "channel": channel, "ts": ts, "limit": "5",
        }).get("messages", []) if m.get("ts") == ts]
    if not messages:
        raise SlackError(f"no message at ts={ts} in {channel}")

    saved = []
    for f in messages[0].get("files") or []:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        name = f.get("name") or f"{f.get('id', 'file')}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {bot_token()}")
        req.add_header("User-Agent", "lab-agent/1.0")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
        except Exception as exc:
            print(f"[slack] could not download {name}: {exc}")
            continue
        if data[:15].lstrip().lower().startswith(b"<!doctype html"):
            print(f"[slack] {name}: got an HTML page, not the file — token lacks files:read?")
            continue
        path = out / name
        path.write_bytes(data)
        saved.append(path)
    return saved


def post_message(channel: str, thread_ts: str | None, text: str) -> None:
    """Fire-and-forget post used by the listener's background threads.

    Logs Slack-level rejections (ok:false) as well as transport errors — the
    previous version checked neither, so a rejected post looked identical to a
    delivered one.
    """
    try:
        post_message_checked(channel, thread_ts, text)
    except SlackError as exc:
        print(f"[post] Failed to post Slack message: {exc}")
