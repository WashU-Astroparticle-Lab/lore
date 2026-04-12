"""
slack_listener.py — Dumb Slack event listener for the lab-agent pipeline.

Listens for @mentions and DMs in Slack. When a message arrives, immediately
acknowledges it, then spawns a fresh Claude Code session to handle the request.
Claude Code reads CLAUDE.md + memory, runs the pipeline, and posts back to Slack.

Required .env vars:
    SLACK_BOT_TOKEN  — xoxb-...  (Bot User OAuth Token)
    SLACK_APP_TOKEN  — xapp-...  (App-Level Token, Socket Mode)

Setup:
    1. Create a Slack app at https://api.slack.com/apps
    2. Enable Socket Mode → generate an App-Level Token (xapp-...) with connections:write scope
    3. Add bot scopes: chat:write, app_mentions:read, im:history
    4. Install app to workspace → copy Bot User OAuth Token (xoxb-...)
    5. Add both tokens to .env
    6. Enable event subscriptions: app_mention, message.im
    7. Run: python slack_listener.py
"""
from __future__ import annotations

import os
import subprocess
import textwrap
import threading
import time
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

# ── Conversation buffer ───────────────────────────────────────────────────────
# Keeps recent messages per user so back-and-forth DMs have context.
# Cleared after CONVERSATION_TIMEOUT seconds of inactivity.

CONVERSATION_TIMEOUT = 5 * 60  # 5 minutes

_conversations: dict[str, list[dict]] = {}  # user_id -> [{"role", "text", "ts"}, ...]
_conv_lock = threading.Lock()

def _get_conversation(user: str) -> list[dict]:
    """Return recent messages for a user, dropping any older than CONVERSATION_TIMEOUT."""
    now = time.time()
    with _conv_lock:
        history = _conversations.get(user, [])
        recent = [m for m in history if now - m["ts"] < CONVERSATION_TIMEOUT]
        _conversations[user] = recent
        return list(recent)

def _append_conversation(user: str, role: str, text: str) -> None:
    with _conv_lock:
        _conversations.setdefault(user, []).append({"role": role, "text": text, "ts": time.time()})

def _format_history(history: list[dict]) -> str:
    if not history:
        return "(no prior messages)"
    lines = []
    for m in history:
        label = "User" if m["role"] == "user" else "Bot"
        lines.append(f"[{label}]: {m['text']}")
    return "\n".join(lines)


# ── Session tracker ───────────────────────────────────────────────────────────
# Keeps a list of active (process, metadata) tuples so we can detect leaks.

MAX_CONCURRENT = 3   # Refuse new requests if this many sessions are already running
SESSION_TIMEOUT = 15 * 60  # Kill any session still running after 15 minutes

_active_sessions: list[dict] = []
_sessions_lock = threading.Lock()

def _reap_finished() -> None:
    """Remove finished processes and kill any that have exceeded SESSION_TIMEOUT."""
    with _sessions_lock:
        for s in list(_active_sessions):
            elapsed = time.time() - s["started_at"]

            # Kill timed-out sessions
            if elapsed > SESSION_TIMEOUT and s["proc"].poll() is None:
                s["proc"].kill()
                print(f"[session-timeout] user={s['user']} killed after {round(elapsed)}s")
                from slack_sdk import WebClient
                WebClient(token=SLACK_BOT_TOKEN).chat_postMessage(
                    channel=s["channel"],
                    thread_ts=s["thread_ts"],
                    text=f"<@{s['user']}> Session timed out after 15 minutes. Please try again.",
                )

            # Remove finished processes
            if s["proc"].poll() is not None:
                elapsed = round(time.time() - s["started_at"])
                print(f"[session-done] user={s['user']} elapsed={elapsed}s exit={s['proc'].returncode}")
                _capture_bot_reply(s["user"], s["log_path"])
                _active_sessions.remove(s)

def _reaper_loop() -> None:
    """Background thread that checks for timed-out/finished sessions every 30 seconds."""
    while True:
        time.sleep(30)
        _reap_finished()

def _active_count() -> int:
    _reap_finished()
    with _sessions_lock:
        return len(_active_sessions)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_pipeline_request(text: str) -> bool:
    """True if the message contains a GitHub URL (pipeline trigger)."""
    return "github.com" in text.lower()

def _should_respond(user: str, text: str) -> bool:
    """
    Decide whether to spawn a Claude session for this message.
    - Always respond to pipeline requests (GitHub URL present)
    - Always respond to the first message in a conversation (no history yet)
    - Ignore short acknowledgments mid-conversation ("ok", "will do", "thanks", etc.)
    """
    if _is_pipeline_request(text):
        return True
    history = _get_conversation(user)
    # No history means this is the first message — always respond
    if not history:
        return True
    # History exists: only respond if the message is a substantive question/statement
    # (more than 4 words and not a pure acknowledgment)
    words = text.strip().split()
    ack_phrases = {"ok", "okay", "sure", "thanks", "thank you", "got it", "will do",
                   "ok will do", "okay will do", "sounds good", "great", "perfect",
                   "noted", "alright", "cool", "nice", "ok sounds good", "okay sounds good"}
    if len(words) <= 6 and text.strip().lower().rstrip("!.,") in ack_phrases:
        return False
    return True


def _spawn_claude(user: str, text: str, channel: str, thread_ts: str) -> None:
    """Spawn a Claude Code session to handle the request, with concurrency guard."""
    # Record incoming user message and get recent history
    _append_conversation(user, "user", text)
    history = _get_conversation(user)
    history_str = _format_history(history)

    prompt = textwrap.dedent(f"""
        You are Lab Agent, the AI assistant for a physics research lab (superconducting qubits and KIDs).
        You live in the lab's Slack workspace and help researchers run experiment report pipelines.
        You are friendly, enthusiastic about physics, and speak naturally — like a knowledgeable labmate,
        not a customer service bot. Keep replies concise and conversational. Use casual language where
        appropriate but stay precise when discussing experiments or data.

        Slack user ID : {user}
        Latest message: {text}
        Reply-to      : channel={channel}, thread_ts={thread_ts}

        Conversation history from the last 5 minutes (oldest first):
        {history_str}

        How to post a Slack message (use this throughout — for progress updates AND final reply):
        Read the token first:
          python -c "from dotenv import dotenv_values; print(dotenv_values('.env')['SLACK_BOT_TOKEN'])"
        Then post:
          curl -s -X POST https://slack.com/api/chat.postMessage \
            -H "Authorization: Bearer <token>" \
            -H "Content-Type: application/json; charset=utf-8" \
            -d '{{"channel":"{channel}","thread_ts":"{thread_ts}","text":"<message>"}}'

        What to do:
        - Read CLAUDE.md in the project root for the full pipeline instructions.
        - Use the conversation history to understand context — don't repeat questions already asked.
        - If info is still missing, ask naturally for just the missing piece — don't list everything again.
        - For questions or chat, answer warmly and helpfully. Keep replies short and conversational.

        If you have a GitHub URL and LabArchives page names, run the full pipeline AND post a
        Slack progress update before each major step:

          1. Before checking .env:
             Post: "Checking credentials..."
          2. Before running run.py:
             Post: "Fetching GitHub and LabArchives data..."
          3. If run.py reports expired cookies, run `python get_la_cookies.py` immediately
             (never ask the user), then post: "Session cookies refreshed, retrying fetch..."
             and rerun run.py.
          4. After run.py completes:
             Post: "Data fetched. Writing report..."
          5. After saving the report file:
             Post: "Report written. Uploading to LabArchives..."
          6. After upload completes, post the final summary reply tagging <@{user}>
             with: what experiment was reported, key findings (2-3 bullets), and confirmation
             it's live in LabArchives under AI Agent.

        After posting the final reply, output it on a line starting with BOT_REPLY: for history tracking.

        Project root: {PROJECT_ROOT}
    """).strip()

    # Guard: refuse if too many sessions are already running
    count = _active_count()
    if count >= MAX_CONCURRENT:
        from slack_sdk import WebClient
        WebClient(token=SLACK_BOT_TOKEN).chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"<@{user}> Sorry, {count} session(s) are already running. Please wait a few minutes and try again.",
        )
        print(f"[session-limit] Rejected request from {user} — {count} active sessions")
        return

    log_path = PROJECT_ROOT / "slack_sessions.log"
    prompt_file = PROJECT_ROOT / f"_prompt_{user}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    with open(log_path, "a") as log:
        log.write(f"\n\n=== New session: user={user} channel={channel} ===\n")
        proc = subprocess.Popen(
            f'claude --dangerously-skip-permissions --output-format text < "{prompt_file}"',
            shell=True,
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=log,
        )

    with _sessions_lock:
        _active_sessions.append({
            "proc": proc,
            "user": user,
            "channel": channel,
            "thread_ts": thread_ts,
            "started_at": time.time(),
            "log_path": log_path,
        })

    print(f"[session-start] user={user} pid={proc.pid} active={_active_count()}")


def _capture_bot_reply(user: str, log_path: Path) -> None:
    """Read the session log and extract the BOT_REPLY: line to store in conversation history."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        for line in reversed(text.splitlines()):
            if line.startswith("BOT_REPLY:"):
                reply = line[len("BOT_REPLY:"):].strip()
                _append_conversation(user, "bot", reply)
                return
    except Exception:
        pass


# ── Event handlers ────────────────────────────────────────────────────────────

@app.event("app_mention")
def handle_mention(event, say, logger):
    """Handles @bot mentions in any channel."""
    user      = event["user"]
    text      = event["text"]
    channel   = event["channel"]
    thread_ts = event.get("thread_ts", event["ts"])

    logger.info(f"Mention from {user}: {text!r}")

    if not _should_respond(user, text):
        logger.info(f"Ignoring acknowledgment from {user}")
        return

    if _is_pipeline_request(text):
        say(text=f"<@{user}> On it — starting now...", thread_ts=thread_ts)

    _spawn_claude(user, text, channel, thread_ts)


@app.event("message")
def handle_dm(event, say, logger):
    """Handles direct messages to the bot."""
    # Skip bot messages and message edits
    if event.get("bot_id") or event.get("subtype"):
        return
    # Only handle DMs (channel type "im")
    if event.get("channel_type") != "im":
        return

    user      = event["user"]
    text      = event["text"]
    channel   = event["channel"]
    thread_ts = event.get("thread_ts", event["ts"])

    logger.info(f"DM from {user}: {text!r}")

    if not _should_respond(user, text):
        logger.info(f"Ignoring acknowledgment from {user}")
        return

    if _is_pipeline_request(text):
        say(text=f"<@{user}> On it — starting now...")

    _spawn_claude(user, text, channel, thread_ts)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[slack-listener] Starting. Project root: {PROJECT_ROOT}")
    print(f"[slack-listener] Session timeout: {SESSION_TIMEOUT // 60} min, max concurrent: {MAX_CONCURRENT}")
    threading.Thread(target=_reaper_loop, daemon=True).start()
    print("[slack-listener] Waiting for Slack messages...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
