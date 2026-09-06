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

from ..config import PROJECT_ROOT
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

KG_REFRESH_HOUR = int(os.environ.get("KB_REFRESH_HOUR", "2"))  # local hour, 0–23


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


def kg_refresh_loop() -> None:
    """Fire an incremental KG refresh once per day at KG_REFRESH_HOUR (local time).

    Checks every 5 minutes; runs at most once per calendar day. A failed refresh
    is logged and retried the next day — it never takes the listener down. The
    build lock makes a same-day double-run (e.g. after a listener restart, or the
    Task Scheduler fallback firing too) safe and near-free.
    """
    last_run_date: tuple[int, int, int] | None = None
    while True:
        now = time.localtime()
        today = (now.tm_year, now.tm_mon, now.tm_mday)
        if now.tm_hour == KG_REFRESH_HOUR and today != last_run_date:
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
    post_data_py: dict = {"channel": channel, "thread_ts": reply_ts, "text": "<message>"}

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

            Delivery rules (unchanged): your final text output is delivered to Slack
            automatically after you exit — do NOT post the final reply yourself. Post
            mid-pipeline progress updates via Python chat.postMessage as before:
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

        ROUTING — decide this BEFORE doing anything else. Classify the latest message:
          (A) REPORT REQUEST — it contains a GitHub URL, OR explicitly asks to write/generate/make
              a report for a specific experiment, OR asks for a DR conditions report.
              -> run the pipeline (see the steps below).
          (B) QUESTION / KNOWLEDGE QUERY — a broad or specific question about the lab's past work:
              "what do we know about...", "have we ever...", "which experiments/runs...", an
              overview/topic question, or any request to find or summarise past findings.
              -> ANSWER FROM THE KNOWLEDGE GRAPH. Run:
                   python -m lab_agent.cli.query_kb "<the user's question>"
                 and reply from its output, citing the experiment/page ids it returns. This is
                 LOCAL and needs NO LabArchives cookies and NO fetching. For a type-(B) question you
                 MUST NOT run run.py, MUST NOT open/fetch LabArchives pages, and MUST NEVER run
                 get_la_cookies.py.
                 RESOLVE IDENTIFIERS FIRST: query_kb prints a "Candidate pages (keyword/ID match)"
                 list below its answer. The graph is weak at raw IDs (chip/run IDs, filenames in
                 hyperlinks, e.g. BE260416); if the graph "doesn't have" an ID from the question but
                 a candidate page clearly matches it, USE that page (for a figure/number question,
                 run fetch_page_images on the top candidate and continue) instead of asking the user
                 to name it. Only if candidates are genuinely ambiguous, list them and ASK WHICH ONE.
                 If NOTHING matches, say so plainly and OFFER a full report — never fabricate,
                 silently fetch, or trigger a cookie refresh.
              EXCEPTION — a question about a specific PLOT/FIGURE: after query_kb finds the page,
                 use the figure step in CLAUDE.md (`fetch_page_images` then Read the images), which
                 is CHEAP-BY-DEFAULT, PRECISE-WHEN-NEEDED: handle qualitative "which/what does this
                 show" reads yourself; for PRECISE/QUANTITATIVE reads (exact values, a mean, reading
                 many points off a plot), or many figures to sift, or low-confidence, or user
                 pushback, delegate the WHOLE figure job to one Opus image-analyst subagent (Agent
                 tool, model: "opus", fresh context) that surveys the figures, ZOOMS the answer
                 figure with `python -m lab_agent.cli.view_figure "<path>" --crop X0 Y0 X1 Y1 --scale
                 2`, then reports per-item values + the computed result + which figure + confidence.
                 That fetch is the one Q&A case that needs the cookie, and you REFRESH IT YOURSELF
                 (like the report pipeline): if fetch_page_images reports COOKIE_REFRESH_NEEDED, post
                 a short Slack heads-up ("Refreshing the LabArchives session — approve the Duo push on
                 your phone"), run `python get_la_cookies.py` yourself, then retry fetch_page_images.
                 (This overrides the "never run get_la_cookies.py" rule, which only applies to the
                 text-only path.) The only human step is the Duo tap; only if the refresh fails/times
                 out do you tell the user it couldn't refresh.
          When unsure, prefer (B): answering from lab knowledge is fast, cheap, and never blocks on cookies.

        IMPORTANT — check thread history before running the pipeline:
        Each Slack reply spawns a fresh session. Read the conversation history carefully to
        determine what stage the pipeline is at before doing anything:

        - If history shows the bot asked about DR conditions and the latest message is the
          user's answer (yes with a date/window, or no): the fetch and Phase A analysis are
          already done. Do NOT re-check credentials, re-run run.py, or re-run Phase A. The
          outputs/<experiment_id>/ files (including extracted_*.md) already exist — resolve
          the DR answer per CLAUDE.md Step 2b and continue from Phase B.

        - If history shows the report was already uploaded, and the user is asking to redo it:
          check whether outputs/<experiment_id>/ already has the data files (labarchives.md,
          notebooks.md, etc.). If yes, skip run.py entirely and go straight to writing a
          fresh report from the existing files.

        - Only run run.py if no output files exist yet, or if the user explicitly asks to
          re-fetch the data.

        Pipeline steps and Slack progress updates (only for a fresh pipeline run):

          All Slack posting uses the CLI — never a hand-written python -c:
             python -m lab_agent.cli.slack post --channel <ch> --thread <ts> --text-file <f>
          Write the text with the Write tool first. Exit 0 means delivered AND verified.

          1. FIRST, before anything else: post the INTAKE questions and continue
             immediately — do NOT wait for answers. Skip any part the request already
             answered, and skip the DR question entirely for bench / room-temperature
             work (spectrum analyser, VNA bench comparison, wiring — anything not in
             the fridge):
               "Starting now — three quick things you can answer while I work:
                1. What question was this experiment trying to answer?
                2. Want dilution refrigerator conditions included? (date/window, or 'no')
                3. Full report or brief?"
             Answer 1 matters most: guessing the objective from the notebooks is what
             causes rewrite cycles later. Answers arrive during Step 5.
          2. Post: "Checking credentials..." then run:
               python -c "from lab_agent.config import check_env; check_env(live=True)"
             This validates the tokens rather than merely finding them. If GITHUB_TOKEN
             is INVALID, say exactly that (and what to do) instead of starting a fetch
             that will fail — a "set but 401" token once burned 75 minutes.
          3. Post: "Fetching GitHub and LabArchives data..." then run run.py.
             If run.py reports expired cookies, run `python get_la_cookies.py` immediately
             (never ask the user), then post: "Session cookies refreshed, retrying fetch..."
             and rerun run.py.
             If run.py exits 4, it refused to overwrite another experiment's directory —
             re-run with --experiment-id <a fresh name>. Do not use --force.
          4. Immediately after run.py completes: post "Data fetched — analyzing..." and
             spawn the Phase A analysts (CLAUDE.md Step 3) WITHOUT waiting for answers.
          5. When Phase A finishes, read the thread for everything the user has said
             since you started:
               python -m lab_agent.cli.slack read-thread --channel <ch> --thread <ts>
             Messages sent while you were working were NOT delivered to you — the
             listener absorbs them — so this read is the only way you will ever see
             them. Treat every one as an instruction you already owe an answer to:
             - the objective (intake Q1) → pass it to Phase B/C as the experiment's goal
             - "full"/"brief" (intake Q3) → pass it to report-writer
             - DR yes + date/window → fetch DR data and run dr-analyst (CLAUDE.md Step 2b)
             - DR no, or a bench experiment → continue
             - ANY other instruction ("only use the two plots from X", "drop section Y")
               → apply it, and acknowledge it in your next post so the user knows it
               landed. Never silently ignore one; a dropped instruction looks identical
               to a followed one from their side.
             - no DR answer yet and DR is relevant → write the Step 2b reminder as your
               final text output and exit; the next session continues from Phase B.
          6. Post: "Writing report..." then run Phases B, C, D per CLAUDE.md.
          7. After saving the report file:
             Post: "Report written. Uploading to LabArchives..."
          8. After upload completes, your final summary is the CONTENTS OF
             <out_dir>/slack_summary.md, verbatim — the report-writer wrote it from the
             report and the critic checked its numbers and hedging against the report.
             Read that file and emit it as your final text output (do NOT post it
             yourself — the system delivers your final output automatically), adding
             only <@{user}> and a one-line timing summary.
             Do NOT compose your own summary from memory of what the subagents
             reported: every factual error that has reached the lab came from that
             (a number paired with the wrong frequency; a "confirming" the critic had
             just removed from the report). If slack_summary.md is missing, re-spawn
             the report-writer rather than writing prose yourself.

        Project root: {PROJECT_ROOT}
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
