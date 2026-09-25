"""
Claude Code session management for the Slack listener: request heuristics,
prompt construction, session spawning, and the reaper that delivers each
session's final output back to Slack.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

from ..config import PROJECT_ROOT, load_env
from . import api
from .history import fetch_history

# ── Session tracker ───────────────────────────────────────────────────────────

MAX_CONCURRENT = 3   # Total concurrent sessions across all users
MAX_PER_USER   = 1   # One active session per user at a time
SESSION_TIMEOUT = 40 * 60  # Kill sessions still running after 40 minutes

_active_sessions: list[dict] = []
_seen_timestamps: set[str] = set()   # dedup: one session per Slack message ts
_sessions_lock   = threading.Lock()
# Requests to re-spawn as a FRESH session after a resume failed. Filled under the lock by the
# reaper; drained outside the lock (spawn_claude re-acquires it) so there is no deadlock.
_pending_retries: list[dict] = []


# ── Claude session persistence (thread_ts → Claude Code session UUID) ─────────
#
# Each Slack thread maps to one long-lived Claude Code session. The first
# message in a thread spawns `claude --session-id <uuid>`; every later reply
# spawns `claude --resume <uuid>`, which restores the full prior conversation
# (tool calls, pipeline stage, outputs already read) instead of cold-starting
# a fresh session that must re-derive everything from thread history.

SESSION_MAP_PATH = PROJECT_ROOT / "session_logs" / "session_map.json"
SESSION_MAP_MAX = 200
_session_map_lock = threading.Lock()


def _load_session_map() -> dict:
    try:
        return json.loads(SESSION_MAP_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_session_map(session_map: dict) -> None:
    SESSION_MAP_PATH.parent.mkdir(exist_ok=True)
    SESSION_MAP_PATH.write_text(
        json.dumps(session_map, indent=2), encoding="utf-8"
    )


def remember_session(thread_key: str, claude_session_id: str) -> None:
    with _session_map_lock:
        session_map = _load_session_map()
        session_map[thread_key] = {
            "claude_session_id": claude_session_id,
            "updated_at": time.time(),
        }
        if len(session_map) > SESSION_MAP_MAX:
            oldest = sorted(session_map, key=lambda k: session_map[k].get("updated_at", 0))
            for k in oldest[: len(session_map) - SESSION_MAP_MAX]:
                session_map.pop(k, None)
        _save_session_map(session_map)


def forget_session(thread_key: str) -> None:
    with _session_map_lock:
        session_map = _load_session_map()
        if session_map.pop(thread_key, None) is not None:
            _save_session_map(session_map)


def _transcript_path(claude_session_id: str) -> Path:
    # Claude Code stores transcripts under ~/.claude/projects/<munged-cwd>/,
    # where the cwd is munged by replacing "/" and "." with "-".
    munged = re.sub(r"[/.]", "-", str(PROJECT_ROOT))
    return Path.home() / ".claude" / "projects" / munged / f"{claude_session_id}.jsonl"


def resumable_session(thread_key: str) -> str | None:
    """Return the stored Claude session ID if its transcript still exists."""
    with _session_map_lock:
        entry = _load_session_map().get(thread_key)
    if not entry:
        return None
    sid = entry.get("claude_session_id", "")
    if sid and _transcript_path(sid).exists():
        return sid
    return None


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
            api.post_message(s["channel"], s["thread_ts"],
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
                        api.post_message(s["channel"], s["thread_ts"], body)
            elif not s.get("timed_out"):
                tail = read_log_tail(log_path) if log_path else ""
                limit_keywords = ("hit your limit", "rate limit", "usage limit",
                                  "too many requests", "overloaded")
                if any(k in tail.lower() for k in limit_keywords):
                    api.post_message(s["channel"], s["thread_ts"],
                        f"<@{s['user']}> I hit the daily token limit mid-pipeline and stopped. "
                        "The report is incomplete — try again after your limit resets.")
                elif s.get("resume_of") and s.get("user_text"):
                    # A RESUMED session failed (the stored Claude session often can't be
                    # resumed after a listener restart / expiry, dying before it writes any
                    # useful log). Don't surface a cryptic error — drop the mapping and
                    # transparently re-run the SAME request as a FRESH session. The retry
                    # has resume_of=None, so if it also fails it falls through to the normal
                    # error path (no retry loop). Queued here, spawned after the lock frees.
                    forget_session(s["thread_key"])
                    _seen_timestamps.discard(s.get("current_ts"))
                    _pending_retries.append({
                        "user": s["user"], "text": s["user_text"], "channel": s["channel"],
                        "thread_ts": s.get("orig_thread_ts"), "current_ts": s.get("current_ts"),
                        "is_dm": s.get("is_dm", False),
                    })
                    print(f"[resume-fallback] resume failed for thread {s['thread_key']}; "
                          "retrying as a fresh session", flush=True)
                else:
                    log_name = Path(log_path).name if log_path else "unknown"
                    api.post_message(s["channel"], s["thread_ts"],
                        f"<@{s['user']}> The session ended unexpectedly (exit {exit_code}). "
                        f"Check session log: `{log_name}`")
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


def _drain_retries() -> None:
    """Spawn any queued fresh-session retries OUTSIDE the lock (spawn_claude re-locks)."""
    while True:
        with _sessions_lock:
            if not _pending_retries:
                return
            spec = _pending_retries.pop(0)
        try:
            spawn_claude(**spec)
        except Exception as exc:  # noqa: BLE001 — a bad retry must not kill the reaper
            print(f"[resume-fallback] retry spawn failed: {exc}", flush=True)


def reap_finished() -> None:
    with _sessions_lock:
        _reap_nolock()
    _drain_retries()


def agent_notes_loop(interval_s: int = 30) -> None:
    """Publish notes the measurement agent queued through LORE's MCP server.

    The server holds no credentials, so it only queues (lab_agent.agent_inbox); this
    listener has the LabArchives keys and uploads them as unreviewed agent notes. A
    failure here is logged and retried; it never takes the listener down.
    """
    from ..publish.agent_notes import process_inbox

    while True:
        try:
            for line in process_inbox():
                print(f"[agent-notes] {line}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[agent-notes] inbox pass failed: {exc}", flush=True)
        time.sleep(interval_s)


def reaper_loop() -> None:
    while True:
        time.sleep(30)
        reap_finished()


# ── Nightly knowledge-graph refresh ────────────────────────────────────────────
#
# The KG must be re-indexed as pages change, but a full rebuild is expensive, so
# `build_kb --index` re-indexes only new/changed pages (manifest-driven; unchanged
# pages cost no LLM tokens). We run that refresh from inside the always-on listener
# so there is one long-lived process to keep alive. The standalone Task Scheduler
# job (LORE-KG-nightly) is kept as a fallback; the cross-process build lock in
# lab_agent.rag.build_lock ensures the two never build at the same time.

DEFAULT_KG_REFRESH_HOUR = 2


def kg_refresh_hour() -> int:
    """The local hour (0-23) for the nightly refresh: KB_REFRESH_HOUR from .env, else 2.

    Read when the refresh loop starts, after loading .env. It used to be a module
    constant, evaluated at import, before anything had loaded .env: the listener only
    loads it on first asking for a Slack token. So KB_REFRESH_HOUR in .env was silently
    ignored and the refresh always ran at 2 a.m., in the middle of an overnight
    measurement that had set it to noon precisely to avoid that.
    """
    load_env()
    raw = os.environ.get("KB_REFRESH_HOUR", "").strip()
    if not raw:
        return DEFAULT_KG_REFRESH_HOUR
    try:
        hour = int(raw)
    except ValueError:
        hour = -1
    if not 0 <= hour <= 23:
        print(f"[kg-refresh] KB_REFRESH_HOUR={raw!r} is not an hour 0-23; using "
              f"{DEFAULT_KG_REFRESH_HOUR:02d}:00.", flush=True)
        return DEFAULT_KG_REFRESH_HOUR
    return hour


def _run_kg_refresh() -> None:
    """Run one incremental KG refresh as a subprocess (plan auth, not API)."""
    # Same env scrub as session spawns: the build LLM (`claude -p`) must use the
    # Claude Code plan, not ANTHROPIC_API_KEY.
    child_env = os.environ.copy()
    for _var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        child_env.pop(_var, None)

    logs_dir = PROJECT_ROOT / "session_logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / "kg_refresh.log"

    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n=== KG refresh started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log.flush()
        proc = subprocess.Popen(
            [sys.executable, "-m", "lab_agent.cli.build_kb", "--index"],
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=log,
            env=child_env,
        )
        proc.wait()
    print(f"[kg-refresh] finished (exit {proc.returncode}); log: {log_path.name}", flush=True)
    # Tell the warm KG service to reload the freshly-rebuilt graph (best-effort; no-op if the
    # service isn't running). Otherwise it would keep serving the pre-refresh graph until restart.
    if proc.returncode == 0:
        try:
            from ..rag.service import trigger_reload
            if trigger_reload():
                print("[kg-refresh] signaled warm KG service to reload.", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[kg-refresh] reload signal failed: {exc}", flush=True)


def kg_refresh_loop(hour: int | None = None) -> None:
    """Fire an incremental KG refresh once per day at ``hour`` (local time).

    ``hour`` defaults to ``kg_refresh_hour()``; the listener passes the value it
    announces at startup, so the log and the schedule cannot disagree.

    Checks every 5 minutes; runs at most once per calendar day. A failed refresh
    is logged and retried the next day — it never takes the listener down. The
    build lock makes a same-day double-run (e.g. after a listener restart, or the
    Task Scheduler fallback firing too) safe and near-free.
    """
    if hour is None:
        hour = kg_refresh_hour()
    last_run_date: tuple[int, int, int] | None = None
    while True:
        now = time.localtime()
        today = (now.tm_year, now.tm_mon, now.tm_mday)
        if now.tm_hour == hour and today != last_run_date:
            last_run_date = today
            print("[kg-refresh] nightly window reached — starting incremental build", flush=True)
            try:
                _run_kg_refresh()
            except Exception as exc:  # noqa: BLE001 — a bad refresh must not kill the listener
                print(f"[kg-refresh] refresh failed: {exc}", flush=True)
        time.sleep(300)  # re-check every 5 minutes


# ── Log tail reader ──────────────────────────────────────────────────────────

def read_log_tail(log_path: Path, lines: int = 30) -> str:
    """Read the last N lines of a session log file."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except Exception:
        return ""


# ── Request heuristics ────────────────────────────────────────────────────────

def is_pipeline_request(text: str) -> bool:
    t = text.lower()
    return (
        "github.com" in t
        or "dr report" in t
        or "dr conditions" in t
        or "dilution refrigerator" in t
    )


def is_ack(text: str) -> bool:
    """True if the message is a short acknowledgment that doesn't need a response."""
    ack_phrases = {
        "ok", "okay", "sure", "thanks", "thank you", "got it", "will do",
        "ok will do", "okay will do", "sounds good", "great", "perfect",
        "noted", "alright", "cool", "nice", "ok sounds good", "okay sounds good",
    }
    words = text.strip().split()
    return len(words) <= 6 and text.strip().lower().rstrip("!.,") in ack_phrases


# ── Core: spawn a Claude session ──────────────────────────────────────────────

def spawn_claude(
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
        history_label = "Thread conversation history (oldest first; new conversation if empty)"
    else:
        # Channel mentions: thread-based. Full thread fetched via conversations.replies.
        reply_ts = thread_ts or current_ts
        history_label = "Conversation history — full thread, oldest first"

    reply_to_str = f"channel={channel}, thread_ts={reply_ts}"

    # Resume the thread's existing Claude session when its transcript survives;
    # a resumed session already holds the full conversation and pipeline state,
    # so it gets a short continuation prompt instead of the cold-start prompt.
    thread_key = str(reply_ts)
    resume_id = resumable_session(thread_key)

    if resume_id:
        prompt = textwrap.dedent(f"""
            (Resumed Slack session — you already have the full context of this conversation
            and any pipeline work you completed earlier. Trust it; do not re-derive state.)

            Latest message from <@{user}>: {text}
            Reply-to      : {reply_to_str}

            Continue from wherever you left off. Never repeat completed steps — if run.py
            outputs or extracted_*.md files already exist, do not regenerate them.

            A request to change a report already written ("make it shorter", "drop the key
            parameters") is a revision of the REPORT — report-writer, critic, upload, summary,
            per experiment-report's revision section. Not an edit of your own reply.

            Delivery rules (unchanged): your final text output is delivered to Slack
            automatically after you exit — do NOT post the final reply yourself. A report's
            final output is <out_dir>/slack_summary.md verbatim, never prose you compose.
            Post mid-pipeline progress updates with the CLI:
              python -m lab_agent.cli.slack post --channel {channel} --thread {reply_ts} --text-file <path>
        """).strip()
        return _launch(user, channel, reply_ts, current_ts, prompt, resume_id=resume_id,
                       thread_key=thread_key, orig_thread_ts=thread_ts, user_text=text,
                       is_dm=is_dm)

    history_str = fetch_history(channel, thread_ts, current_ts)

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
        Project root  : {PROJECT_ROOT}

        {history_label} (empty means this is a new conversation):
        {history_str}

        FIRST ACTION, before you answer anything: read CLAUDE.md in the project root, classify
        the request per its routing table, and invoke the matching skill with the Skill tool.
        CLAUDE.md and .claude/skills/ are the only pipeline instructions — this prompt does not
        restate them, and deliberately so. It used to, and the copy went stale: it was still
        telling sessions to ask "Full report or brief?" after that question was removed for
        causing full-template reports, and still telling them to "go straight to writing a fresh
        report from the existing files" when the skill forbids writing a report outside
        report-writer. A session answered a revision request with zero tool calls, twice,
        because this prompt's routing had no case for it and its own text was easier to reach
        than the file.

        Answering from this prompt alone is the failure mode. If the request touches an
        experiment, a report, a figure or the lab's past work, you cannot answer it correctly
        without the skill — the history above is a record of the conversation, not a source of
        facts about the data.

        A request to CHANGE a report already written ("make it shorter", "drop the key
        parameters", "the goal was really X") is a revision **of the report**, not an edit of
        your own reply. It runs the full path in experiment-report's revision section:
        report-writer, then the critic, then the upload, then the summary. Shortening your Slack
        message changes nothing the user asked about.

        How your reply is delivered:
        Your final text output is posted to Slack automatically after you exit. Do NOT post the
        final reply yourself — that double-posts. For a report, your final output is the verbatim
        contents of <out_dir>/slack_summary.md, plus only <@{user}> and a one-line timing
        summary. Read that file; never compose a summary from what the subagents told you.
        (`slack post-summary` is for sending a summary to a CHANNEL, where nothing auto-posts.)

        For mid-pipeline progress updates, use the CLI (never a hand-written python -c with a
        token in it, and never curl):
          python -m lab_agent.cli.slack post --channel {channel} --thread {reply_ts} --text-file <path>
        Write the message body with the Write tool into .lore_tmp/ first.

        Everything else — credentials, run.py, the phase agents, the gate, the upload, Slack
        search, cookie refresh — is in CLAUDE.md and the skill you invoke. Go read it.
    """).strip()

    _launch(user, channel, reply_ts, current_ts, prompt, resume_id=None,
            thread_key=thread_key, session_id=session_id, orig_thread_ts=thread_ts,
            user_text=text, is_dm=is_dm)


def _launch(
    user: str,
    channel: str,
    reply_ts: str | None,
    current_ts: str,
    prompt: str,
    *,
    resume_id: str | None,
    thread_key: str,
    session_id: str | None = None,
    orig_thread_ts: str | None = None,
    user_text: str = "",
    is_dm: bool = False,
) -> None:
    """Reserve a session slot and spawn the claude process (fresh or resumed).

    ``orig_thread_ts``/``user_text``/``is_dm`` are the original request inputs, stashed on the
    slot so the reaper can re-spawn a fresh session verbatim if a resume fails.
    """
    session_id = session_id or uuid.uuid4().hex[:8]

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

        # A session for this same thread is still running — stay silent. The
        # running session polls the thread for replies (that is how the DR
        # answer arrives mid-pipeline), so a notice here would just be noise.
        if any(s.get("thread_key") == thread_key for s in _active_sessions):
            print(f"[session-skip] Thread {thread_key} already has an active session; "
                  "the reply will be picked up by the running session.", flush=True)
            return

        # Global cap
        active_count = len(_active_sessions)
        if active_count >= MAX_CONCURRENT:
            api.post_message(channel, reply_ts,
                             f"<@{user}> Sorry, {active_count} session(s) are already running. "
                             "Please wait a few minutes and try again.")
            print(f"[session-limit] Rejected {user} — {active_count} active (global cap)", flush=True)
            return

        # Per-user cap
        user_count = sum(1 for s in _active_sessions if s["user"] == user)
        if user_count >= MAX_PER_USER:
            api.post_message(channel, reply_ts,
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
            "thread_key": thread_key,
            "resume_of":  resume_id,
            "current_ts": current_ts,
            "orig_thread_ts": orig_thread_ts,
            "user_text":  user_text,
            "is_dm":      is_dm,
        }
        _active_sessions.append(slot)

    # ── Spawn outside lock ────────────────────────────────────────────────────
    logs_dir = PROJECT_ROOT / "session_logs"
    logs_dir.mkdir(exist_ok=True)
    session_log = logs_dir / f"session_{session_id}_{user}.log"

    prompt_file = PROJECT_ROOT / f"_prompt_{user}_{session_id}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # The spawned Claude Code session must authenticate with the Claude Code plan
    # (claude.ai / enterprise login), NOT the Anthropic API. If ANTHROPIC_API_KEY
    # (or another API auth token) is present in the environment, Claude Code uses
    # it in preference to the plan login — which fails here because the API account
    # is not the billing path we want. Scrub those vars from the child environment
    # so the session falls back to the plan login. The pipeline's own scripts read
    # their keys directly from .env via load_dotenv, so this does not affect them.
    child_env = os.environ.copy()
    for _var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        child_env.pop(_var, None)

    # Resume the thread's prior Claude session if we have one; otherwise mint a
    # session UUID ourselves so later replies in this thread can resume it.
    if resume_id:
        claude_session_id = resume_id
        session_flags = f"--resume {claude_session_id}"
    else:
        claude_session_id = str(uuid.uuid4())
        session_flags = f"--session-id {claude_session_id}"

    with open(session_log, "w") as log:
        log.write(f"=== Session {session_id}: user={user} channel={channel} "
                  f"claude_session={claude_session_id} resumed={bool(resume_id)} ===\n")
        proc = subprocess.Popen(
            f'claude --dangerously-skip-permissions --output-format text '
            f'--model claude-sonnet-4-6 {session_flags} < "{prompt_file}"',
            shell=True,
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=log,
            env=child_env,
        )

    remember_session(thread_key, claude_session_id)

    # Fill in the slot now that we have the real proc
    slot["proc"]        = proc
    slot["session_log"] = session_log
    slot["prompt_file"] = prompt_file

    print(f"[session-start] user={user} session={session_id} pid={proc.pid} "
          f"resumed={bool(resume_id)} active={len(_active_sessions)}", flush=True)
