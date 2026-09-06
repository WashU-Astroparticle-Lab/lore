"""
Slack event listener for the lab-agent pipeline (Bolt app + handlers).

Listens for @mentions and DMs in Slack. When a message arrives, immediately
acknowledges it, then spawns a fresh Claude Code session to handle the request
(see sessions.py). Claude Code reads CLAUDE.md + memory, runs the pipeline,
and its final output is posted back to Slack by the session reaper.

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
"""
from __future__ import annotations

import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from ..config import PROJECT_ROOT
from . import api
from .sessions import (
    KG_REFRESH_HOUR,
    MAX_CONCURRENT,
    MAX_PER_USER,
    SESSION_TIMEOUT,
    is_ack,
    is_pipeline_request,
    kg_refresh_loop,
    reaper_loop,
    spawn_claude,
)

app = App(token=api.bot_token())


# ── Event handlers ────────────────────────────────────────────────────────────

@app.event("app_mention")
def handle_mention(event, say, logger):
    user        = event["user"]
    text        = api.unwrap_slack_text(event["text"])
    channel     = event["channel"]
    current_ts  = event["ts"]
    thread_ts   = event.get("thread_ts")   # None if this is the thread parent

    logger.info(f"Mention from {user}: {text!r}")

    if is_ack(text):
        logger.info(f"Ignoring acknowledgment from {user}")
        return

    if is_pipeline_request(text):
        say(text=f"<@{user}> On it — starting now...", thread_ts=thread_ts or current_ts)

    spawn_claude(user, text, channel, thread_ts, current_ts)


@app.event("message")
def handle_dm(event, say, logger):
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") != "im":
        return

    user       = event["user"]
    text       = api.unwrap_slack_text(event["text"])
    channel    = event["channel"]
    current_ts = event["ts"]
    thread_ts  = event.get("thread_ts")

    logger.info(f"DM from {user}: {text!r}")

    if is_ack(text):
        logger.info(f"Ignoring acknowledgment from {user}")
        return

    if is_pipeline_request(text):
        ack_thread = thread_ts or current_ts
        api.post_message(channel, ack_thread, f"<@{user}> On it — starting now...")

    spawn_claude(user, text, channel, thread_ts, current_ts, is_dm=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    api.set_bot_user_id(api.fetch_bot_user_id())
    print(f"[slack-listener] Starting. Project root: {PROJECT_ROOT}")
    print(f"[slack-listener] Bot user ID: {api.BOT_USER_ID or '(unknown)'}")
    print(f"[slack-listener] Session timeout: {SESSION_TIMEOUT // 60} min  "
          f"Max concurrent: {MAX_CONCURRENT}  Per-user cap: {MAX_PER_USER}", flush=True)
    threading.Thread(target=reaper_loop, daemon=True).start()
    threading.Thread(target=kg_refresh_loop, daemon=True).start()
    print(f"[slack-listener] Nightly KG refresh scheduled for {KG_REFRESH_HOUR:02d}:00 "
          "(local); Task Scheduler job kept as fallback, build lock prevents overlap.", flush=True)
    # Host the warm knowledge-graph query service so Slack Q&A skips the ~25-40s cold start
    # per question. It loads the graph once in its own thread; query_kb falls back to a cold
    # load if it isn't up. No-op if the RAG deps / built graph are absent.
    from ..rag.service import serve_in_background
    serve_in_background()
    print("[slack-listener] Waiting for Slack messages...")
    SocketModeHandler(app, api.app_token()).start()


if __name__ == "__main__":
    main()
