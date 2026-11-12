"""
Claude Code session management for the Slack listener: request heuristics,
prompt construction, session spawning, and the reaper that delivers each
session's final output back to Slack.
"""
from __future__ import annotations

import difflib
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
from . import api, people
from .history import (bot_asked_last, fetch_channel_before, fetch_history,
                      fetch_thread_since)

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
# Requests that could not start yet: their thread already had a session running, or the
# user or the listener was at its cap. They used to be dropped, one of them after "I'll
# reply when it finishes", which nothing kept. The reaper starts them once there is room.
_waiting: list[dict] = []
MAX_WAITING = 20
# A session that finds a message already dealt with writes this as its reply: nothing posts.
NO_REPLY = "(no reply)"
# The model every Slack session runs on; LORE_SESSION_MODEL in .env overrides it.
DEFAULT_SESSION_MODEL = "claude-sonnet-5-5"


def session_model() -> str:
    return os.environ.get("LORE_SESSION_MODEL") or DEFAULT_SESSION_MODEL
# A resumed session that fails sooner than this most likely could not be resumed at all, so
# it is re-run fresh. One that fails later had been working: re-running it from scratch could
# repeat an upload, so it gets the normal error instead.
RESUME_FAILED_FAST_S = 120


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


def project_dir_name(path: str) -> str:
    """The folder Claude Code keeps a directory's transcripts in, under ~/.claude/projects/.

    Every character that is not a letter or digit becomes "-":
    ``C:\\Users\\LC Control\\Desktop\\Lab-Agent-main\\LORE`` ->
    ``C--Users-LC-Control-Desktop-Lab-Agent-main-LORE``. Replacing only "/" and "." left
    a Windows path whole, so the lookup landed in LORE\\<id>.jsonl, and no thread on the
    lab PC was ever resumed: 57 sessions, every follow-up starting from scratch.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _transcript_path(claude_session_id: str) -> Path:
    return (Path.home() / ".claude" / "projects" / project_dir_name(str(PROJECT_ROOT))
            / f"{claude_session_id}.jsonl")


def last_turn_at(thread_key: str) -> float | None:
    """When the thread's previous session started (epoch s), or None if it has none."""
    with _session_map_lock:
        entry = _load_session_map().get(thread_key)
    return float(entry["updated_at"]) if entry and entry.get("updated_at") else None


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


# A session is told its final output is posted for it, and CLAUDE.md's Slack section
# tells it anything the user should see must be posted and confirmed. Some sessions
# obey the second: they post the answer with `cli.slack post`, then end with "Message
# is confirmed in the thread. Here's the answer: ..." and the reaper posted that too.
_DUPLICATE_RATIO = 0.6
_ALREADY_POSTED_PREAMBLE = re.compile(
    r"^[^\n]{0,120}\b(confirmed|posted|sent|delivered)\b[^\n]{0,120}\n+", re.IGNORECASE)


def _normalise(text: str) -> str:
    """Letters and digits only, so Slack's reformatting (** → *) cannot hide a match."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.lower()).split())[:4000]


def duplicates_earlier_post(body: str, earlier: list[str]) -> bool:
    """Whether ``body`` is essentially a message the bot already posted.

    A leading "confirmed / posted in the thread" line is ignored, since that is the
    shape of the duplicate. Short outputs are never judged duplicates: too little
    text to tell, and a missing reply is worse than a repeated one.
    """
    b = _normalise(_ALREADY_POSTED_PREAMBLE.sub("", body.strip(), count=1))
    if len(b) < 60:
        return False
    for text in earlier:
        e = _normalise(text)
        if e and difflib.SequenceMatcher(None, b, e, autojunk=False).ratio() >= _DUPLICATE_RATIO:
            return True
    return False


def _bot_posts_since(channel: str, thread_ts: str, since: float) -> list[str]:
    """Texts the bot posted in the thread after ``since`` (epoch seconds).

    Slack returns a thread's parent whatever ``oldest`` says, so the time is checked here.
    Without that, a revised summary posted under the bot's own channel summary was compared
    with that summary, judged a duplicate, and never posted.
    """
    body = api.api_get("conversations.replies", {
        "channel": channel, "ts": thread_ts, "oldest": f"{since:.6f}", "limit": "100"})
    return [m.get("text", "") for m in body.get("messages", [])
            if float(m.get("ts") or 0) >= since
            and ((api.BOT_USER_ID and m.get("user") == api.BOT_USER_ID) or m.get("bot_id"))]


# Slack rejects a message over 40,000 characters; split well under that, at paragraphs.
SLACK_CHUNK = 35_000
POST_ATTEMPTS = 3


def split_for_slack(text: str, limit: int = SLACK_CHUNK) -> list[str]:
    """``text`` in pieces Slack accepts, split at paragraph breaks where possible."""
    chunks, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    return chunks + ([rest] if rest.strip() else [])


def _post_final(channel: str, thread_ts: str, body: str) -> bool:
    """Post a session's answer, retrying a failed post. True once every piece is in.

    It used to be a single fire-and-forget post: one 429 or timeout and an answer that took
    twenty minutes was gone, its reply file deleted a moment later.
    """
    for piece in split_for_slack(body):
        for attempt in range(1, POST_ATTEMPTS + 1):
            try:
                api.post_message_checked(channel, thread_ts, piece)
                break
            except api.SlackError as exc:
                print(f"[session-done] post attempt {attempt}/{POST_ATTEMPTS} failed: {exc}",
                      flush=True)
                if attempt < POST_ATTEMPTS:
                    time.sleep(attempt)
        else:
            return False
    return True


def _deliver_final_output(s: dict, body: str) -> None:
    """Post a finished session's output, unless the session already posted it itself."""
    try:
        earlier = _bot_posts_since(s["channel"], s["thread_ts"], s["started_at"])
    except Exception as exc:  # noqa: BLE001 — can't check: post; a repeat beats silence
        print(f"[session-done] could not check the thread for an earlier post ({exc}); "
              "posting the final output.", flush=True)
        earlier = []
    if duplicates_earlier_post(body, earlier):
        print(f"[session-done] session={s['session_id']} already posted its answer in the "
              "thread; not posting the final output again.", flush=True)
        if s.get("progress") == "status":
            _set_status(s, "")   # no post follows to clear it
        return
    if not _post_final(s["channel"], s["thread_ts"], body):
        kept = PROJECT_ROOT / "session_logs" / f"undelivered_{s['session_id']}.md"
        try:
            kept.write_text(body, encoding="utf-8")
        except OSError:
            kept = None
        print(f"[session-done] session={s['session_id']} could not post its answer; "
              f"kept in {kept}", flush=True)
        if s.get("progress") == "status":
            _set_status(s, "")


# ── The reply file ────────────────────────────────────────────────────────────
#
# A session's reply is the file it writes to reply_path(session_id), not its stdout.
# Stdout carries whatever the model said on the way, and on 2026-10-20 the lab got it:
# "I have enough context now. Let me compose the response. ... here's a thorough reply:
# ---" above one answer, and under it a second turn, woken by a background query that
# finished late: "The response has already been posted to the thread. Nothing further
# needed." Stdout is now only the fallback for a session that wrote no file, cleaned of
# the shapes those leaks took.

REPLY_DIR = PROJECT_ROOT / ".lore_tmp"

_WAITING = re.compile(r"\bwaiting for\b", re.IGNORECASE)
_RULE = re.compile(r"-{3,}|\*{3,}|_{3,}")
_ALREADY_SENT = re.compile(
    r"\balready (?:been )?(?:posted|delivered|sent)\b|\bnothing further\b", re.IGNORECASE)


def reply_path(session_id: str) -> Path:
    return REPLY_DIR / f"reply_{session_id}.md"


def remove_stale_reply_files() -> int:
    """Delete reply files no session will claim: those left when a listener died mid-session.

    Called at listener start, when no session is running. Each reaped session deletes its
    own, so this only catches what a crash or restart left behind.
    """
    removed = 0
    for p in REPLY_DIR.glob("reply_*.md"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def clean_stdout_reply(text: str) -> str:
    """Strip the narration a session's stdout carries around its answer.

    Three shapes, each from a reply the lab received: leading "Waiting for the
    github-analyst..." turns from background agents; a lead-in ending in a colon and a
    "---" rule ("Here's my final reply to the user:"); trailing turns saying the answer is
    already out. Anything else is left alone, and if nothing would remain the text is
    returned whole: a cluttered reply is better than none.
    """
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    while paras and len(paras[0]) <= 300 and _WAITING.search(paras[0]):
        paras.pop(0)
    for i, p in enumerate(paras[:4]):
        if _RULE.fullmatch(p.strip()):
            lead_in = "\n\n".join(paras[:i])
            if i and lead_in.rstrip().endswith(":") and len(lead_in) <= 400:
                paras = paras[i + 1:]
            break
    while paras and len(paras[-1]) <= 400 and _ALREADY_SENT.search(paras[-1]):
        paras.pop()
    cleaned = "\n\n".join(paras).strip()
    return cleaned or text.strip()


def _reply_file_text(s: dict) -> str:
    """The session's reply file, stripped; "" if it wrote none."""
    reply_file = s.get("reply_file")
    if not reply_file:
        return ""
    try:
        return Path(reply_file).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _reply_body(s: dict) -> str:
    """What to post for a finished session: its reply file, else its stdout, cleaned.

    A reply file holding only NO_REPLY means the session decided nothing needs saying.
    """
    text = _reply_file_text(s)
    if text == NO_REPLY:
        return ""
    if text:
        return text
    log_path = s.get("session_log")
    if not log_path or not Path(log_path).exists():
        return ""
    lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    raw = "\n".join(lines[1:] if lines and lines[0].startswith("=== Session") else lines).strip()
    if raw:
        print(f"[session-done] session={s['session_id']} wrote no reply file; posting its "
              "output, cleaned of narration.", flush=True)
    return clean_stdout_reply(raw) if raw else ""


def _kill_tree(proc) -> None:
    """End a session and everything it started.

    The session runs as ``cmd /c claude ...`` (shell=True), and on Windows ``proc.kill()``
    ends only cmd.exe. claude.exe carried on: still using the plan, able to post or upload,
    its answer never delivered, and resumable by the next message while still writing the
    same transcript.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        except Exception as exc:  # noqa: BLE001 — fall through to the plain kill
            print(f"[session-timeout] taskkill failed ({exc})", flush=True)
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 — already gone
        pass


def _reap_nolock() -> None:
    """Reap finished/timed-out sessions. Caller must hold _sessions_lock."""
    now = time.time()
    for s in list(_active_sessions):
        if s["proc"] is None:
            continue  # placeholder not yet filled
        elapsed = now - s["started_at"]
        if elapsed > SESSION_TIMEOUT and s["proc"].poll() is None:
            _kill_tree(s["proc"])
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
            written = _reply_file_text(s)
            if exit_code == 0:
                # Deliver the session's reply (its reply file; stdout as a fallback). The
                # listener posts it so the session doesn't need to post its answer itself.
                body = _reply_body(s)
                if body:
                    _deliver_final_output(s, body)
                elif s.get("progress") == "status":
                    _set_status(s, "")   # nothing to post, so nothing would clear it
            elif written and not s.get("timed_out"):
                # It wrote its answer and then failed, e.g. on a late background-task turn.
                # The answer is still the answer; it used to be deleted unposted.
                print(f"[session-done] session={s['session_id']} exited {exit_code} after "
                      "writing its reply; posting the reply.", flush=True)
                if written != NO_REPLY:
                    _deliver_final_output(s, written)
            elif not s.get("timed_out"):
                tail = read_log_tail(log_path) if log_path else ""
                limit_keywords = ("hit your limit", "rate limit", "usage limit",
                                  "too many requests", "overloaded")
                if any(k in tail.lower() for k in limit_keywords):
                    api.post_message(s["channel"], s["thread_ts"],
                        f"<@{s['user']}> I hit the daily token limit mid-pipeline and stopped. "
                        "The report is incomplete — try again after your limit resets.")
                elif (s.get("resume_of") and s.get("user_text")
                      and now - s["started_at"] < RESUME_FAILED_FAST_S):
                    # A RESUMED session failed at once (the stored Claude session often can't
                    # be resumed after a listener restart / expiry, dying before it writes any
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
            # Clean up the prompt and reply files now that the session is over
            for key in ("prompt_file", "reply_file"):
                f = s.get(key)
                if f and Path(f).exists():
                    try:
                        Path(f).unlink()
                    except Exception:
                        pass

    # Prune seen-timestamp set to prevent unbounded growth
    if len(_seen_timestamps) > 500:
        excess = sorted(_seen_timestamps)[: len(_seen_timestamps) - 250]
        for ts in excess:
            _seen_timestamps.discard(ts)


def _can_start(spec: dict) -> bool:
    """Whether a waiting request would pass _launch's checks now. Caller holds the lock."""
    return (not any(s.get("thread_key") == spec["thread_key"] for s in _active_sessions)
            and sum(1 for s in _active_sessions if s["user"] == spec["user"]) < MAX_PER_USER
            and len(_active_sessions) < MAX_CONCURRENT)


def _drain_retries() -> None:
    """Spawn queued retries and waiting requests OUTSIDE the lock (spawn_claude re-locks)."""
    while True:
        with _sessions_lock:
            if _pending_retries:
                spec, kind = _pending_retries.pop(0), "retry"
            else:
                ready = next((w for w in _waiting if _can_start(w)), None)
                if ready is None:
                    return
                _waiting.remove(ready)
                _seen_timestamps.discard(ready["current_ts"])
                spec = {k: v for k, v in ready.items() if k != "thread_key"}
                spec["requeued"] = True
                kind = "waiting"
        try:
            spawn_claude(**spec)
        except Exception as exc:  # noqa: BLE001 — a bad retry must not kill the reaper
            print(f"[{'resume-fallback' if kind == 'retry' else 'queue'}] spawn failed: {exc}",
                  flush=True)


def reap_finished() -> None:
    with _sessions_lock:
        _reap_nolock()
    _drain_retries()
    _progress_tick()


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
# so there is one long-lived process to keep alive. A Task Scheduler job (LORE-KG-nightly)
# can run the same build as a fallback, though the lab PC has none; the cross-process build
# lock in lab_agent.rag.build_lock ensures two builders never run at the same time.

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


KG_REFRESH_TIMEOUT_S = int(os.environ.get("KB_REFRESH_TIMEOUT_H", "4")) * 3600
_KG_EXIT_MEANING = {0: "ok", 3: "SKIPPED, RAG packages missing (wrong Python?)",
                    4: "skipped, another build holds the lock",
                    5: "partial, some pages not indexed (see the log)"}


SLACK_ARCHIVE_TIMEOUT_S = 2 * 3600


def _run_slack_archive() -> None:
    """Run the daily Slack archive (lab_agent.cli.slack_archive) as a bounded subprocess."""
    child_env = os.environ.copy()
    for _var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        child_env.pop(_var, None)
    logs_dir = PROJECT_ROOT / "session_logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / "slack_archive.log"
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n=== Slack archive started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log.flush()
        proc = subprocess.Popen([sys.executable, "-m", "lab_agent.cli.slack_archive"],
                                cwd=str(PROJECT_ROOT), stdout=log, stderr=log, env=child_env)
        try:
            proc.wait(timeout=SLACK_ARCHIVE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait(timeout=60)
            log.write("\n=== Slack archive killed after 2 h ===\n")
    outcome = {0: "ok", 5: "partial, some discussions not posted (see the log)"}.get(
        proc.returncode, "failed")
    print(f"[slack-archive] finished: {outcome} (exit {proc.returncode}); log: {log_path.name}",
          flush=True)


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
        try:
            # Bounded: a build that hung on 2026-10-22 held this thread for six days, and no
            # refresh ran until the listener was stopped.
            proc.wait(timeout=KG_REFRESH_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait(timeout=60)
            log.write(f"\n=== KG refresh killed after {KG_REFRESH_TIMEOUT_S // 3600} h ===\n")
    outcome = _KG_EXIT_MEANING.get(proc.returncode, "failed")
    print(f"[kg-refresh] finished: {outcome} (exit {proc.returncode}); log: {log_path.name}",
          flush=True)
    # build_kb tells the warm service to reload when it changed the graph.


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
            # Archive the day's Slack figures first, so their week pages are in LabArchives
            # when the refresh crawls it and flow into the graph in this same run.
            try:
                _run_slack_archive()
            except Exception as exc:  # noqa: BLE001 — a failed archive must not skip the refresh
                print(f"[slack-archive] run failed: {exc}", flush=True)
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


def ignorable_ack(text: str, channel: str, thread_ts: str | None, current_ts: str) -> bool:
    """An "ok"/"thanks" that needs no answer: one after an answer, not after a question.

    is_ack alone dropped "Sure!" sent in reply to "Send this to #x, or tell me what to
    change?", so the approved message was never sent and the user heard nothing back.
    """
    return is_ack(text) and not bot_asked_last(channel, thread_ts, current_ts)


_acked: set[str] = set()


def claim_ack(ts: str) -> bool:
    """True the first time a message ts asks for an instant ack, False after.

    The listener acks before _launch dedups, so a DM @mention (which fires two events) or a
    redelivered event got "On it" twice.
    """
    with _sessions_lock:
        if ts in _acked:
            return False
        _acked.add(ts)
        if len(_acked) > 500:
            for old in sorted(_acked)[:250]:
                _acked.discard(old)
        return True


def acknowledgement(text: str) -> str | None:
    """The message to post the instant a request arrives, or None.

    Only a report request gets one: it starts a long pipeline. A question used to get
    "Looking into it..." too, and a reply on every question was noise; questions are
    covered by the progress signals instead (``_progress_tick``).
    """
    if is_pipeline_request(text):
        return "On it — starting now..."
    return None


# ── Progress signals while a session runs ──────────────────────────────────────
#
# A three-minute answer with no sign of life was reported as "the listener has
# paused". In a DM (a Slack assistant thread) LORE shows Slack's own "is thinking..."
# status: no message, and Slack clears it when the answer is posted. It is refreshed
# every STATUS_REFRESH_S so a long answer does not outlive it. Where the status is
# not available (channels, or a DM Slack refuses it for), a question still running
# after SLOW_NOTE_AFTER_S gets one short note, so a quick answer gets nothing extra.

THINKING_STATUS = "is thinking…"
STATUS_REFRESH_S = 90
SLOW_NOTE_AFTER_S = 60
SLOW_NOTE = "Still working on this — answers can take a few minutes."


def _set_status(s: dict, status: str = THINKING_STATUS) -> bool:
    """Set (or, with "", clear) the assistant-thread status. False if Slack refuses."""
    try:
        api.api_post("assistant.threads.setStatus", {
            "channel_id": s["channel"], "thread_ts": s["thread_ts"], "status": status})
        return True
    except Exception as exc:  # noqa: BLE001 — a missing status must never affect the answer
        print(f"[progress] could not set thread status ({exc})", flush=True)
        return False


def _initial_progress(is_dm: bool, user_text: str) -> str:
    if is_dm:
        return "status"
    return "none" if is_pipeline_request(user_text) else "slow_note"


def _progress_tick(now: float | None = None) -> None:
    """Refresh DM statuses and send any due slow-answer notes, for running sessions."""
    now = time.time() if now is None else now
    with _sessions_lock:
        running = [s for s in _active_sessions
                   if s.get("proc") is not None and s["proc"].poll() is None]
    for s in running:
        if s.get("progress") == "status" and now - s.get("status_set_at", 0) >= STATUS_REFRESH_S:
            if _set_status(s):
                s["status_set_at"] = now
            else:
                s["progress"] = "none" if is_pipeline_request(s.get("user_text", "")) else "slow_note"
        if (s.get("progress") == "slow_note" and not s.get("slow_note_sent")
                and now - s["started_at"] >= SLOW_NOTE_AFTER_S):
            s["slow_note_sent"] = True
            try:
                api.post_message(s["channel"], s["thread_ts"], SLOW_NOTE)
            except Exception as exc:  # noqa: BLE001
                print(f"[progress] could not post the slow-answer note ({exc})", flush=True)


# ── Core: spawn a Claude session ──────────────────────────────────────────────

def spawn_claude(
    user: str,
    text: str,
    channel: str,
    thread_ts: str | None,
    current_ts: str,
    is_dm: bool = False,
    arrived_while_busy: bool = False,
    requeued: bool = False,
) -> None:
    """Spawn a Claude Code session to handle the request.

    ``arrived_while_busy``: the message came in while an earlier session was working in the
    same thread, and waited for it. ``requeued``: this is that waiting request starting now,
    so a cap it meets again queues it without another notice.
    """
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
    reply_file = reply_path(session_id)
    busy_note = (
        f"This message arrived while you were still working on your previous turn in this "
        f"thread, and waited for it to finish. If that turn already used or answered it, "
        f"write exactly {NO_REPLY} as the reply file and nothing will be posted."
        if arrived_while_busy else "")

    if resume_id:
        # The resumed session remembers its own turns, not what others said in between: a
        # colleague's reply in a channel thread wakes nobody, and a message that waited for
        # the previous turn is not in its transcript. Before resuming worked, every follow-up
        # re-read the whole thread, so this is what keeps that context.
        since = last_turn_at(thread_key)
        missed = (fetch_thread_since(channel, reply_ts, since, current_ts) if since
                  else "") or "(none)"
        prompt = textwrap.dedent(f"""
            (Resumed Slack session — you already have the full context of this conversation
            and any pipeline work you completed earlier. Trust it; do not re-derive state.)

            Messages posted in this thread by others since your last turn, oldest first:
            {missed}

            Latest message from {people.label(user)}: {people.named(text)}
            Reply-to      : {reply_to_str}
            {busy_note}

            Continue from wherever you left off. Never repeat completed steps — if run.py
            outputs or extracted_*.md files already exist, do not regenerate them.

            A request to change a report already written ("make it shorter", "drop the key
            parameters") is a revision of the REPORT — report-writer, critic, upload, summary,
            per experiment-report's revision section. Not an edit of your own reply.

            Delivery rules: your reply this time is the file {reply_file} (a new path: the
            reply file from earlier turns is gone). As your LAST step, write the whole reply there
            with the Write tool; it is posted to Slack verbatim after you exit, and nothing you
            print is. Put nothing in it but the reply: no "Here's my reply:" lead-in, no "---",
            no note on which skill you picked. If a background task finishes after you wrote
            it, rewrite the file only if the result changes the answer. Do NOT post the reply
            yourself. A report's reply is <out_dir>/slack_summary.md verbatim, never prose you
            compose. Post mid-pipeline progress updates with the CLI:
              python -m lab_agent.cli.slack post --channel {channel} --thread {reply_ts} --text-file <path>
        """).strip()
        return _launch(user, channel, reply_ts, current_ts, prompt, resume_id=resume_id,
                       thread_key=thread_key, session_id=session_id, orig_thread_ts=thread_ts,
                       user_text=text, is_dm=is_dm, arrived_while_busy=arrived_while_busy,
                       requeued=requeued)

    history_str = fetch_history(channel, thread_ts, current_ts)
    channel_str = ""
    if not is_dm and thread_ts is None:
        recent = fetch_channel_before(channel, current_ts)
        if recent:
            channel_str = ("Recent messages in this channel just before the mention, oldest "
                           "first (what the mention may refer to):\n" + recent)
    sender = people.label(user)
    first = people.first_name(user)
    talking_to = (
        f"You are talking to {first}, the sender above. Talk to them directly. Never tell "
        f"them to ask themselves, and never credit anyone with work no record shows. If they "
        f"ask on behalf of someone else in the conversation, answer so that person can use "
        f"it too." if first else
        "The sender's name is not known; answer them directly, and never credit anyone "
        "with work no record shows.")

    prompt = textwrap.dedent(f"""
        You are LORE, the AI assistant for a physics research lab (superconducting qubits and KIDs).
        You live in the lab's Slack workspace and help researchers run experiment report pipelines.
        You are friendly, enthusiastic about physics, and speak naturally — like a knowledgeable labmate,
        not a customer service bot. Keep replies concise and conversational. Use casual language where
        appropriate but stay precise when discussing experiments or data.

        From          : {sender}
        Session ID    : {session_id}
        Latest message: {people.named(text)}
        Reply-to      : {reply_to_str}
        Project root  : {PROJECT_ROOT}
        {busy_note}

        {talking_to}

        {history_label} (empty means this is a new conversation):
        {history_str}

        {channel_str}

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

        Small talk — a greeting, "you working?", thanks — matches no row of the routing table
        and needs no skill: just write the reply file.

        How your reply is delivered:
        Your reply is the file {reply_file}. As your LAST step, write the whole reply there with
        the Write tool; the listener posts that file to the thread, verbatim, after you exit.
        Nothing you print is posted — your printed output used to be, and the lab received
        "I have enough context now. Let me compose the response." above an answer. So the file
        holds the reply and nothing else: no "Here's my reply:" lead-in, no "---", no note on
        which skill you picked or why. Write it when the answer is final. If a background task
        finishes after that, rewrite the file only if the result changes the answer; never
        write that the answer was already sent. Do NOT post the reply yourself — that
        double-posts. For a report, the reply is the verbatim contents of
        <out_dir>/slack_summary.md, plus only <@{user}> and a one-line timing summary. Read
        that file; never compose a summary from what the subagents told you.
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
            user_text=text, is_dm=is_dm, arrived_while_busy=arrived_while_busy,
            requeued=requeued)


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
    arrived_while_busy: bool = False,
    requeued: bool = False,
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

        # Busy thread or a cap: wait for room instead of dropping the message. A message
        # sent while its thread's session ran was dropped on the theory that the session
        # polls the thread, which only the report pipeline does, once. It waits in silence
        # (the running session may yet read it) and is told it may already be answered.
        def wait(note: str | None, busy: bool) -> None:
            if len(_waiting) >= MAX_WAITING:
                api.post_message(channel, reply_ts, f"<@{user}> I'm too backed up to take this "
                                 "right now. Please send it again in a few minutes.")
                print(f"[queue] full; rejected {user} ts={current_ts}", flush=True)
                return
            _waiting.append({"user": user, "text": user_text, "channel": channel,
                             "thread_ts": orig_thread_ts, "current_ts": current_ts,
                             "is_dm": is_dm, "thread_key": thread_key,
                             "arrived_while_busy": busy or arrived_while_busy})
            if note and not requeued:
                api.post_message(channel, reply_ts, f"<@{user}> {note}")
            print(f"[queue] {user} ts={current_ts} waits ({len(_waiting)} waiting)", flush=True)

        if any(s.get("thread_key") == thread_key for s in _active_sessions):
            wait(None, busy=True)
            return

        # Global cap
        active_count = len(_active_sessions)
        if active_count >= MAX_CONCURRENT:
            wait(f"{active_count} requests are already running; I'll start yours as soon as "
                 "one finishes.", busy=False)
            return

        # Per-user cap
        user_count = sum(1 for s in _active_sessions if s["user"] == user)
        if user_count >= MAX_PER_USER:
            wait("You already have a request running; I'll start this one as soon as it "
                 "finishes.", busy=False)
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
            "reply_file": reply_path(session_id),
            "timed_out":  False,
            "thread_key": thread_key,
            "resume_of":  resume_id,
            "current_ts": current_ts,
            "orig_thread_ts": orig_thread_ts,
            "user_text":  user_text,
            "is_dm":      is_dm,
            "progress":   _initial_progress(is_dm, user_text),
        }
        _active_sessions.append(slot)

    # ── Spawn outside lock ────────────────────────────────────────────────────
    # If anything here raises, give the slot back. A reserved slot with no process was
    # skipped by the reaper forever: that thread and user stayed "busy" until a restart.
    try:
        _spawn_into(slot, prompt, user=user, channel=channel, session_id=session_id,
                    resume_id=resume_id)
    except Exception as exc:  # noqa: BLE001
        with _sessions_lock:
            if slot in _active_sessions:
                _active_sessions.remove(slot)
            _seen_timestamps.discard(current_ts)
        print(f"[session-start] could not start a session for {user}: {exc}", flush=True)
        api.post_message(channel, reply_ts, f"<@{user}> I couldn't start working on that "
                         f"({type(exc).__name__}). Please send it again.")
        return
    try:
        remember_session(thread_key, slot["claude_session_id"])
    except Exception as exc:  # noqa: BLE001 — the session runs; only resuming it is lost
        print(f"[session-start] could not record session for resume: {exc}", flush=True)

    print(f"[session-start] user={user} session={session_id} pid={slot['proc'].pid} "
          f"resumed={bool(resume_id)} active={len(_active_sessions)}", flush=True)
    _progress_tick()   # sets the DM "thinking" status now, not at the next reaper pass


GUARDED_TOOLS = "Bash|PowerShell|Read|Grep|Glob|Edit|Write|NotebookEdit"


def session_settings() -> Path:
    """The settings file every Slack session is started with: the hook that keeps it off
    the raw fridge logs (lab_agent/slack/session_guard.py).

    Passed with --settings rather than kept in .claude/settings.json, so it binds LORE's
    sessions only and not a person working in this folder.
    """
    guard = Path(__file__).with_name("session_guard.py")
    hook = {"type": "command",
            "command": f'"{Path(sys.executable).as_posix()}" "{guard.as_posix()}"'}
    path = REPLY_DIR / "session_settings.json"
    REPLY_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": GUARDED_TOOLS, "hooks": [hook]}]}}, indent=1), encoding="utf-8")
    return path


def _spawn_into(slot: dict, prompt: str, *, user: str, channel: str, session_id: str,
                resume_id: str | None) -> None:
    """Write the prompt, start claude, and fill the reserved slot."""
    logs_dir = PROJECT_ROOT / "session_logs"
    logs_dir.mkdir(exist_ok=True)
    session_log = logs_dir / f"session_{session_id}_{user}.log"
    REPLY_DIR.mkdir(exist_ok=True)

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

    settings = session_settings()
    with open(session_log, "w") as log:
        log.write(f"=== Session {session_id}: user={user} channel={channel} "
                  f"claude_session={claude_session_id} resumed={bool(resume_id)} ===\n")
        proc = subprocess.Popen(
            f'claude --dangerously-skip-permissions --output-format text '
            f'--model {session_model()} --settings "{settings}" {session_flags} '
            f'< "{prompt_file}"',
            shell=True,
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=log,
            env=child_env,
        )

    # Fill in the slot the moment the process exists, so nothing later can strand it.
    slot["proc"]        = proc
    slot["session_log"] = session_log
    slot["prompt_file"] = prompt_file
    slot["claude_session_id"] = claude_session_id
    # Reap the moment the session exits. Waiting for the 30 s reaper pass held every
    # reply back by up to 30 s — a greeting answered in 8 s showed up seconds later.
    threading.Thread(target=_reap_on_exit, args=(proc,), daemon=True).start()


def _reap_on_exit(proc: subprocess.Popen) -> None:
    proc.wait()
    try:
        reap_finished()
    except Exception as exc:  # noqa: BLE001 — the periodic reaper will retry
        print(f"[session-done] immediate reap failed ({exc}); the reaper will retry.",
              flush=True)
