"""
slack_listener.py — Slack event listener for the lab-agent pipeline.

Listens for @mentions and DMs in Slack. When a message arrives, immediately
acknowledges it, then spawns a fresh Claude Code session to handle the request.
Claude Code reads CLAUDE.md + memory, runs the pipeline, and posts back to Slack.

Required .env vars:
    SLACK_BOT_TOKEN  — xoxb-...  (Bot User OAuth Token)
    SLACK_APP_TOKEN  — xapp-...  (App-Level Token, Socket Mode)

Setup:
    1. Create a Slack app at https://api.slack.com/apps
    2. Enable Socket Mode → generate an App-Level Token (xapp-...) with connections:write scope
    3. Add bot scopes: chat:write, app_mentions:read, im:history, channels:history,
                       groups:history, search:read, channels:read, groups:read
    4. Install app to workspace → copy Bot User OAuth Token (xoxb-...)
    5. Add both tokens to .env
    6. Enable event subscriptions: app_mention, message.im
    7. Run: python slack_listener.py

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

import json
import os
import subprocess
import textwrap
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ── Load credentials ──────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.resolve()
load_dotenv(PROJECT_ROOT / ".env", override=True)

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]

app = App(token=SLACK_BOT_TOKEN)


# ── Bot identity ──────────────────────────────────────────────────────────────
# Fetched once at startup so we can label the bot's own messages as "Bot:"
# in the history string we build for Claude.

def _get_bot_user_id() -> str:
    req = urllib.request.Request("https://slack.com/api/auth.test")
    req.add_header("Authorization", f"Bearer {SLACK_BOT_TOKEN}")
    req.add_header("User-Agent", "lab-agent/1.0")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("user_id", "")
    except Exception as exc:
        print(f"[init] Could not fetch bot user ID: {exc}")
        return ""

BOT_USER_ID: str = ""  # populated in main()


# ── Slack history fetch ───────────────────────────────────────────────────────

def _slack_get(endpoint: str, params: dict[str, str]) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"https://slack.com/api/{endpoint}?{qs}")
    req.add_header("Authorization", f"Bearer {SLACK_BOT_TOKEN}")
    req.add_header("User-Agent", "lab-agent/1.0")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"[history] Slack API call failed ({endpoint}): {exc}")
        return {}


def _fetch_dm_history(channel: str, current_ts: str, limit: int = 30) -> str:
    """Return recent top-level DM messages as a formatted string (unused in normal flow).

    Kept for ad-hoc use. Normal DM sessions use _fetch_history with the thread_ts.
    """
    data = _slack_get("conversations.history", {
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
        text = msg.get("text", "").strip()
        if not text:
            continue
        is_bot = bool(msg.get("bot_id")) or msg.get("user") == BOT_USER_ID
        label = "Bot" if is_bot else "User"
        lines.append(f"[{label}]: {text}")

    return "\n".join(lines) if lines else "(new conversation)"


def _fetch_history(channel: str, thread_ts: str | None, current_ts: str) -> str:
    """Return thread history for channel @mentions via conversations.replies.

    If thread_ts is None this is a new top-level mention — no prior context.
    """
    if thread_ts is None:
        return "(new conversation)"

    data = _slack_get("conversations.replies", {
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
        text = msg.get("text", "").strip()
        if not text:
            continue
        is_bot = bool(msg.get("bot_id")) or msg.get("user") == BOT_USER_ID
        label = "Bot" if is_bot else "User"
        lines.append(f"[{label}]: {text}")

    return "\n".join(lines) if lines else "(new conversation)"



# ── Session tracker ───────────────────────────────────────────────────────────

MAX_CONCURRENT = 3   # Total concurrent sessions across all users
MAX_PER_USER   = 1   # One active session per user at a time
SESSION_TIMEOUT = 40 * 60  # Kill sessions still running after 40 minutes

_active_sessions: list[dict] = []
_seen_timestamps: set[str] = set()   # dedup: one session per Slack message ts
_sessions_lock   = threading.Lock()


def _reap_nolock() -> None:
    """Reap finished/timed-out sessions. Caller must hold _sessions_lock."""
    now = time.time()
    for s in list(_active_sessions):
        if s["proc"] is None:
            continue  # placeholder not yet filled
        elapsed = now - s["started_at"]
        if elapsed > SESSION_TIMEOUT and s["proc"].poll() is None:
            s["proc"].kill()
            s["timed_out"] = True
            print(f"[session-timeout] user={s['user']} session={s['session_id']} "
                  f"killed after {round(elapsed)}s", flush=True)
            _post_message(s["channel"], s["thread_ts"],
                          f"<@{s['user']}> Session timed out after "
                          f"{SESSION_TIMEOUT // 60} minutes. Please try again.")
        if s["proc"].poll() is not None:
            exit_code = s["proc"].returncode
            elapsed_r = round(now - s["started_at"])
            print(f"[session-done] user={s['user']} session={s['session_id']} "
                  f"elapsed={elapsed_r}s exit={exit_code}", flush=True)
            log_path = s.get("session_log")
            if exit_code == 0:
                # Deliver Claude's final output to Slack directly from the log.
                # Claude writes its response to stdout (captured in log); the
                # listener posts it so Claude doesn't need to call curl itself.
                if log_path and Path(log_path).exists():
                    raw = Path(log_path).read_text(encoding="utf-8", errors="replace")
                    lines = raw.splitlines()
                    body = "\n".join(lines[1:] if lines and lines[0].startswith("=== Session") else lines).strip()
                    if body:
                        _post_message(s["channel"], s["thread_ts"], body)
            elif not s.get("timed_out"):
                tail = _read_log_tail(log_path) if log_path else ""
                limit_keywords = ("hit your limit", "rate limit", "usage limit",
                                  "too many requests", "overloaded")
                if any(k in tail.lower() for k in limit_keywords):
                    msg = (f"<@{s['user']}> I hit the daily token limit mid-pipeline and stopped. "
                           "The report is incomplete — try again after your limit resets.")
                else:
                    log_name = Path(log_path).name if log_path else "unknown"
                    msg = (f"<@{s['user']}> The session ended unexpectedly (exit {exit_code}). "
                           f"Check session log: `{log_name}`")
                _post_message(s["channel"], s["thread_ts"], msg)
            _active_sessions.remove(s)
            # Clean up the prompt file now that the session is over
            pf = s.get("prompt_file")
            if pf and Path(pf).exists():
                try:
                    Path(pf).unlink()
                except Exception:
                    pass

    # Prune seen-timestamp set to prevent unbounded growth
    if len(_seen_timestamps) > 500:
        excess = sorted(_seen_timestamps)[: len(_seen_timestamps) - 250]
        for ts in excess:
            _seen_timestamps.discard(ts)


def _reap_finished() -> None:
    with _sessions_lock:
        _reap_nolock()


def _reaper_loop() -> None:
    while True:
        time.sleep(30)
        _reap_finished()


# ── Slack posting helper ──────────────────────────────────────────────────────

def _post_message(channel: str, thread_ts: str | None, text: str) -> None:
    d: dict = {"channel": channel, "text": text}
    if thread_ts:
        d["thread_ts"] = thread_ts
    payload = json.dumps(d)
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload.encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {SLACK_BOT_TOKEN}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", "lab-agent/1.0")
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as exc:
        print(f"[post] Failed to post Slack message: {exc}")


# ── Log tail reader ──────────────────────────────────────────────────────────

def _read_log_tail(log_path: Path, lines: int = 30) -> str:
    """Read the last N lines of a session log file."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except Exception:
        return ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_pipeline_request(text: str) -> bool:
    t = text.lower()
    return (
        "github.com" in t
        or "dr report" in t
        or "dr conditions" in t
        or "dilution refrigerator" in t
    )


def _is_ack(text: str) -> bool:
    """True if the message is a short acknowledgment that doesn't need a response."""
    ack_phrases = {
        "ok", "okay", "sure", "thanks", "thank you", "got it", "will do",
        "ok will do", "okay will do", "sounds good", "great", "perfect",
        "noted", "alright", "cool", "nice", "ok sounds good", "okay sounds good",
    }
    words = text.strip().split()
    return len(words) <= 6 and text.strip().lower().rstrip("!.,") in ack_phrases


# ── Core: spawn a Claude session ──────────────────────────────────────────────

def _spawn_claude(
    user: str,
    text: str,
    channel: str,
    thread_ts: str | None,
    current_ts: str,
    is_dm: bool = False,
) -> None:
    """Spawn a Claude Code session to handle the request."""
    session_id = uuid.uuid4().hex[:8]

    if is_dm:
        # New top-level message → thread to it (anchors a fresh conversation).
        # Reply within an existing thread → continue in that thread.
        # History is scoped to this thread only; top-level messages start fresh.
        reply_ts: str | None = thread_ts if thread_ts else current_ts
        history_str = _fetch_history(channel, thread_ts, current_ts)
        history_label = "Thread conversation history (oldest first; new conversation if empty)"
        reply_to_str = f"channel={channel}, thread_ts={reply_ts}"
        post_data_py: dict = {"channel": channel, "thread_ts": reply_ts, "text": "<message>"}
    else:
        # Channel mentions: thread-based. Full thread fetched via conversations.replies.
        reply_ts = thread_ts or current_ts
        history_str  = _fetch_history(channel, thread_ts, current_ts)
        history_label = "Conversation history — full thread, oldest first"
        reply_to_str = f"channel={channel}, thread_ts={reply_ts}"
        post_data_py: dict = {"channel": channel, "thread_ts": reply_ts, "text": "<message>"}

    prompt = textwrap.dedent(f"""
        You are LORE, the AI assistant for a physics research lab (superconducting qubits and KIDs).
        You live in the lab's Slack workspace and help researchers run experiment report pipelines.
        You are friendly, enthusiastic about physics, and speak naturally — like a knowledgeable labmate,
        not a customer service bot. Keep replies concise and conversational. Use casual language where
        appropriate but stay precise when discussing experiments or data.

        Slack user ID : {user}
        Session ID    : {session_id}
        Latest message: {text}
        Reply-to      : {reply_to_str}

        {history_label} (empty means this is a new conversation):
        {history_str}

        IMPORTANT — how responses are delivered:
        Your final response is delivered to Slack automatically by the system after you exit.
        Just write your response as normal text output. Do NOT try to post your final reply
        to Slack yourself.

        For MID-PIPELINE progress updates only (e.g. "Fetching data...", "Writing report..."),
        post using Python — do NOT use curl (it is unreliable on Windows):
          python -c "
import json, urllib.request
from dotenv import dotenv_values
token = dotenv_values('{PROJECT_ROOT}/.env')['SLACK_BOT_TOKEN']
data = json.dumps({post_data_py}).encode()
req = urllib.request.Request('https://slack.com/api/chat.postMessage', data=data, method='POST')
req.add_header('Authorization', f'Bearer {{token}}')
req.add_header('Content-Type', 'application/json')
urllib.request.urlopen(req, timeout=10)
"

        Searching Slack for experiment context (only when the user's request is vague and you need
        to find a GitHub URL, LabArchives page name, or experiment name — not for general browsing):
          python -c "
import json, urllib.request, urllib.parse
from dotenv import dotenv_values
token = dotenv_values('{PROJECT_ROOT}/.env')['SLACK_BOT_TOKEN']
qs = urllib.parse.urlencode({{'query': '<term>', 'count': '5'}})
req = urllib.request.Request(f'https://slack.com/api/search.messages?{{qs}}')
req.add_header('Authorization', f'Bearer {{token}}')
with urllib.request.urlopen(req, timeout=10) as r: print(r.read().decode())
"

        What to do:
        - Read CLAUDE.md in the project root for the full pipeline instructions.
        - The conversation history above is the live record of this Slack chat, fetched directly
          from Slack right now. "(new conversation)" means the user started a fresh chat — clean slate.
        - You also have persistent memory files on disk (in memory/ next to CLAUDE.md) that carry
          background knowledge across sessions: project state, pipeline facts, rules, etc.
        - When the user asks "what do you remember?" or similar, answer only from the conversation
          history above. Do not surface or mention persistent memory files — those are background
          context for you, not something to recite to the user.
        - Don't repeat questions that are already answered in the history.
        - If info is still missing, ask naturally for just the missing piece.
        - For questions or chat, answer warmly and helpfully. Keep replies short and conversational.

        IMPORTANT — check thread history before running the pipeline:
        Each Slack reply spawns a fresh session. Read the conversation history carefully to
        determine what stage the pipeline is at before doing anything:

        - If history shows "Data fetched" and then the bot asked about DR conditions,
          and the latest message is the user's answer (yes/no): this session's job is ONLY
          to handle the DR question and continue from there. Do NOT re-check credentials or
          re-run run.py. The output files already exist in outputs/ — read them and write
          the report (or fetch DR data first if the user said yes).

        - If history shows the report was already uploaded, and the user is asking to redo it:
          check whether outputs/<experiment_id>/ already has the data files (labarchives.md,
          notebooks.md, etc.). If yes, skip run.py entirely and go straight to writing a
          fresh report from the existing files.

        - Only run run.py if no output files exist yet, or if the user explicitly asks to
          re-fetch the data.

        Pipeline steps and Slack progress updates (only for a fresh pipeline run):

          1. Before checking .env:
             Post: "Checking credentials..."
          2. Before running run.py:
             Post: "Fetching GitHub and LabArchives data..."
          3. If run.py reports expired cookies, run `python get_la_cookies.py` immediately
             (never ask the user), then post: "Session cookies refreshed, retrying fetch..."
             and rerun run.py.
          4. After run.py completes (IMPORTANT — DR conditions pause):
             Write ONLY this as your final text output and exit immediately. Do not post
             anything via Python. Do not add any other text before or after.
               "Pipeline finished. Would you like to include dilution refrigerator conditions
               in this report? If yes, please give me the date and time window of your
               measurement (e.g. 'Feb 18 2025' or 'Feb 18 2025, 14:00–22:00')."
             The system delivers this to the user. The next session reads their reply and
             continues. Do NOT write "I've posted..." or any other meta-commentary.
          5. (Next session, after user answers DR question) Before running Phase A:
             Post: "Writing report..."
          6. After saving the report file:
             Post: "Report written. Uploading to LabArchives..."
          7. After upload completes, write your final summary as normal text output
             (do NOT post it yourself — the system delivers your final output automatically).
             Include: what experiment was reported, key findings (2-3 bullets), confirmation
             it's live in LabArchives under AI Agent, tagging <@{user}>.

        Project root: {PROJECT_ROOT}
    """).strip()

    # ── Atomic dedup + cap check ─────────────────────────────────────────────
    # Holding the lock for the entire check-and-reserve block prevents two
    # concurrent Slack events (e.g. app_mention + message.im fired by the same
    # DM @mention) from both passing the guard and spawning duplicate sessions.
    slot: dict = {}
    with _sessions_lock:
        _reap_nolock()

        # Deduplicate: one session per Slack message timestamp
        if current_ts in _seen_timestamps:
            print(f"[dedup] Dropped duplicate event ts={current_ts} user={user}", flush=True)
            return
        _seen_timestamps.add(current_ts)

        # Global cap
        active_count = len(_active_sessions)
        if active_count >= MAX_CONCURRENT:
            _post_message(channel, reply_ts,
                          f"<@{user}> Sorry, {active_count} session(s) are already running. "
                          "Please wait a few minutes and try again.")
            print(f"[session-limit] Rejected {user} — {active_count} active (global cap)", flush=True)
            return

        # Per-user cap
        user_count = sum(1 for s in _active_sessions if s["user"] == user)
        if user_count >= MAX_PER_USER:
            _post_message(channel, reply_ts,
                          f"<@{user}> You already have a session running — I'll reply when it finishes.")
            print(f"[session-limit] Rejected {user} — already has an active session", flush=True)
            return

        # Reserve the slot immediately so concurrent events see it before proc starts
        slot = {
            "proc":       None,
            "user":       user,
            "channel":    channel,
            "thread_ts":  reply_ts,
            "started_at": time.time(),
            "session_id": session_id,
            "session_log": None,
            "prompt_file": None,
            "timed_out":  False,
        }
        _active_sessions.append(slot)

    # ── Spawn outside lock ────────────────────────────────────────────────────
    logs_dir = PROJECT_ROOT / "session_logs"
    logs_dir.mkdir(exist_ok=True)
    session_log = logs_dir / f"session_{session_id}_{user}.log"

    prompt_file = PROJECT_ROOT / f"_prompt_{user}_{session_id}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    with open(session_log, "w") as log:
        log.write(f"=== Session {session_id}: user={user} channel={channel} ===\n")
        proc = subprocess.Popen(
            f'claude --dangerously-skip-permissions --output-format text --model claude-sonnet-4-6 < "{prompt_file}"',
            shell=True,
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=log,
        )

    # Fill in the slot now that we have the real proc
    slot["proc"]        = proc
    slot["session_log"] = session_log
    slot["prompt_file"] = prompt_file

    print(f"[session-start] user={user} session={session_id} pid={proc.pid} "
          f"active={len(_active_sessions)}", flush=True)


# ── Event handlers ────────────────────────────────────────────────────────────

@app.event("app_mention")
def handle_mention(event, say, logger):
    user        = event["user"]
    text        = event["text"]
    channel     = event["channel"]
    current_ts  = event["ts"]
    thread_ts   = event.get("thread_ts")   # None if this is the thread parent

    logger.info(f"Mention from {user}: {text!r}")

    if _is_ack(text):
        logger.info(f"Ignoring acknowledgment from {user}")
        return

    if _is_pipeline_request(text):
        say(text=f"<@{user}> On it — starting now...", thread_ts=thread_ts or current_ts)

    _spawn_claude(user, text, channel, thread_ts, current_ts)


@app.event("message")
def handle_dm(event, say, logger):
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") != "im":
        return

    user       = event["user"]
    text       = event["text"]
    channel    = event["channel"]
    current_ts = event["ts"]
    thread_ts  = event.get("thread_ts")

    logger.info(f"DM from {user}: {text!r}")

    if _is_ack(text):
        logger.info(f"Ignoring acknowledgment from {user}")
        return

    if _is_pipeline_request(text):
        ack_thread = thread_ts or current_ts
        _post_message(channel, ack_thread, f"<@{user}> On it — starting now...")

    _spawn_claude(user, text, channel, thread_ts, current_ts, is_dm=True)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    BOT_USER_ID = _get_bot_user_id()
    print(f"[slack-listener] Starting. Project root: {PROJECT_ROOT}")
    print(f"[slack-listener] Bot user ID: {BOT_USER_ID or '(unknown)'}")
    print(f"[slack-listener] Session timeout: {SESSION_TIMEOUT // 60} min  "
          f"Max concurrent: {MAX_CONCURRENT}  Per-user cap: {MAX_PER_USER}", flush=True)
    threading.Thread(target=_reaper_loop, daemon=True).start()
    print("[slack-listener] Waiting for Slack messages...")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
