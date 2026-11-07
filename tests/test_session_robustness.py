"""The listener must not lose, drop or double-post a request.

Each test is a failure found in the 2026-11-06 review of the live setup:
  * a timed-out session's claude.exe kept running (only cmd.exe was killed);
  * "Sure!" answering the bot's own question was dropped as an acknowledgement;
  * a message sent while its thread's session ran was dropped, and "I'll reply when it
    finishes" was promised but nothing was queued;
  * a resumed follow-up saw only the message that woke it, not the thread since;
  * a resumed session that failed late re-ran the whole request from scratch;
  * an answer whose one post failed was deleted unposted;
  * the duplicate check compared the answer with the thread's parent;
  * a session that could not start leaked its slot forever.

Run: python tests/run_all.py session_robustness
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
import time
from pathlib import Path

from lab_agent.slack import history, sessions as s


@contextlib.contextmanager
def patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


@contextlib.contextmanager
def clean_state():
    saved = (list(s._active_sessions), set(s._seen_timestamps), list(s._pending_retries),
             list(s._waiting))
    s._active_sessions.clear(); s._seen_timestamps.clear()
    s._pending_retries.clear(); s._waiting.clear()
    try:
        yield
    finally:
        s._active_sessions[:] = saved[0]
        s._seen_timestamps.clear(); s._seen_timestamps.update(saved[1])
        s._pending_retries[:] = saved[2]
        s._waiting[:] = saved[3]


class _Proc:
    def __init__(self, code=None, pid=4242):
        self.returncode, self.pid, self.killed = code, pid, False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True


def _slot(**over):
    base = dict(proc=_Proc(0), user="U1", channel="D1", thread_ts="1.0", started_at=time.time(),
                session_id="sid", session_log=None, prompt_file=None, reply_file=None,
                timed_out=False, thread_key="1.0", resume_of=None, current_ts="1.5",
                orig_thread_ts="1.0", user_text="what is Qc?", is_dm=True, progress="none")
    base.update(over)
    return base


def test_a_timeout_kills_the_whole_process_tree():
    calls = []
    proc = _Proc(None, pid=777)
    with patched(s.subprocess, run=lambda args, **kw: calls.append(args)), \
         patched(s.sys, platform="win32"):
        s._kill_tree(proc)
    assert calls == [["taskkill", "/T", "/F", "/PID", "777"]], calls
    assert proc.killed


def test_the_duplicate_check_ignores_the_threads_older_parent():
    parent = {"ts": "100.0", "bot_id": "B1", "text": "the summary"}
    later = {"ts": "300.0", "bot_id": "B1", "text": "a progress post"}
    with patched(s.api, api_get=lambda *a, **k: {"messages": [parent, later]}):
        assert s._bot_posts_since("C1", "100.0", 200.0) == ["a progress post"]


def test_a_failed_post_is_retried_and_long_answers_are_split():
    long = ("para one\n\n" + "x" * 40) * 3
    pieces = s.split_for_slack(long, limit=60)
    assert len(pieces) > 1 and all(len(p) <= 60 for p in pieces), pieces
    attempts = []

    def flaky(ch, ts, text):
        attempts.append(text)
        if len(attempts) < 3:
            raise s.api.SlackError("chat.postMessage: ratelimited")
        return "9.9"

    with patched(s.api, post_message_checked=flaky), patched(s.time, sleep=lambda *_: None):
        assert s._post_final("D1", "1.0", "short answer") is True
    assert len(attempts) == 3, "a 429 on the first two tries must not lose the answer"


def test_an_answer_that_cannot_be_posted_is_kept():
    def down(*a):
        raise s.api.SlackError("transport error")

    with tempfile.TemporaryDirectory() as d, \
         patched(s, PROJECT_ROOT=Path(d), _bot_posts_since=lambda *a: []), \
         patched(s.api, post_message_checked=down), patched(s.time, sleep=lambda *_: None):
        (Path(d) / "session_logs").mkdir()
        s._deliver_final_output(_slot(session_id="keepme"), "the twenty-minute answer")
        kept = Path(d) / "session_logs" / "undelivered_keepme.md"
        assert kept.read_text(encoding="utf-8") == "the twenty-minute answer"


def test_a_resumed_session_is_shown_the_thread_since_its_last_turn():
    msgs = [{"ts": "100.0", "user": "U1", "text": "parent, older than the last turn"},
            {"ts": "250.0", "user": "U2", "text": "use run 5"},
            {"ts": "260.0", "bot_id": "B1", "text": "a bot progress post"},
            {"ts": "270.0", "user": "U1", "text": "go ahead"}]
    with patched(history.api, slack_get=lambda *a, **k: {"ok": True, "messages": msgs},
                 BOT_USER_ID="UBOT"):
        out = history.fetch_thread_since("C1", "100.0", 200.0, current_ts="270.0")
    assert out == "[<@U2>]: use run 5", out

    launched = []
    with patched(s, resumable_session=lambda k: "claude-1", last_turn_at=lambda k: 200.0,
                 fetch_thread_since=lambda *a: "[<@U2>]: use run 5",
                 _launch=lambda *a, **kw: launched.append(a[4])):
        s.spawn_claude("U1", "go ahead", "C1", "100.0", "270.0")
    assert "[<@U2>]: use run 5" in launched[0], launched[0][:400]


def test_a_reply_to_the_bots_question_is_not_dropped():
    asked = [{"ts": "1.0", "user": "U1", "text": "post the summary"},
             {"ts": "2.0", "bot_id": "B1", "text": "Send this to #data, or tell me what to change?"}]
    answered = asked[:1] + [{"ts": "2.0", "bot_id": "B1", "text": "Done, it's posted."}]
    with patched(history.api, slack_get=lambda *a, **k: {"ok": True, "messages": asked},
                 BOT_USER_ID="UBOT"):
        assert not s.ignorable_ack("Sure!", "D1", "1.0", "3.0"), "a yes to a question was dropped"
    with patched(history.api, slack_get=lambda *a, **k: {"ok": True, "messages": answered},
                 BOT_USER_ID="UBOT"):
        assert s.ignorable_ack("thanks", "D1", "1.0", "3.0")
    with patched(history.api, slack_get=lambda *a, **k: {}):
        assert not s.ignorable_ack("ok", "D1", "1.0", "3.0"), "unreadable thread: answer it"
    assert s.ignorable_ack("ok", "D1", None, "3.0"), "a top-level ok is still ignored"


def test_a_message_sent_while_its_thread_is_busy_waits_and_then_runs():
    posted, spawned = [], []
    with clean_state(), patched(s.api, post_message=lambda *a: posted.append(a)), \
         patched(s, spawn_claude=lambda **kw: spawned.append(kw)):
        s._active_sessions.append(_slot(proc=_Proc(None), current_ts="1.2"))
        s._launch("U1", "D1", "1.0", "1.7", "prompt", resume_id=None, thread_key="1.0",
                  orig_thread_ts="1.0", user_text="also check channel 6", is_dm=True)
        assert len(s._waiting) == 1 and not posted, "it must wait, silently"
        s._active_sessions[0]["proc"].returncode = 0
        s._active_sessions[0]["user_text"] = "first"
        s.reap_finished()
    assert len(spawned) == 1, spawned
    assert spawned[0]["text"] == "also check channel 6" and spawned[0]["arrived_while_busy"]
    assert spawned[0]["requeued"] and spawned[0]["current_ts"] == "1.7"


def test_the_per_user_cap_queues_and_says_so_once():
    posted = []
    with clean_state(), patched(s.api, post_message=lambda *a: posted.append(a[2])):
        s._active_sessions.append(_slot(proc=_Proc(None), thread_key="9.0", current_ts="9.0"))
        s._launch("U1", "D1", "5.0", "5.0", "p", resume_id=None, thread_key="5.0",
                  user_text="second question", is_dm=True)
        assert len(s._waiting) == 1 and len(posted) == 1 and "as soon as" in posted[0]
        s._seen_timestamps.discard("5.0")
        s._launch("U1", "D1", "5.0", "5.0", "p", resume_id=None, thread_key="5.0",
                  user_text="second question", is_dm=True, requeued=True)
        assert len(posted) == 1, "a requeued request must not announce itself again"


def test_only_a_resume_that_fails_at_once_is_rerun_fresh():
    posted = []
    for started, expect_retry in ((time.time(), True), (time.time() - 600, False)):
        with clean_state(), patched(s.api, post_message=lambda *a: posted.append(a[2])), \
             patched(s, forget_session=lambda k: None, spawn_claude=lambda **kw: None):
            s._active_sessions.append(_slot(proc=_Proc(1), resume_of="old", started_at=started))
            s._reap_nolock()
            assert bool(s._pending_retries) == expect_retry, (started, s._pending_retries)
    assert any("ended unexpectedly" in p for p in posted), "a late failure must be reported"


def test_a_reply_written_before_a_failed_exit_is_posted():
    posted = []
    with tempfile.TemporaryDirectory() as d, clean_state(), \
         patched(s, _bot_posts_since=lambda *a: []), \
         patched(s.api, post_message_checked=lambda ch, ts, t: posted.append(t) or "1",
                 post_message=lambda *a: posted.append(a[2])):
        reply = Path(d) / "reply.md"
        reply.write_text("the answer", encoding="utf-8")
        s._active_sessions.append(_slot(proc=_Proc(1), reply_file=reply))
        s._reap_nolock()
    assert posted == ["the answer"], posted


def test_no_reply_means_nothing_is_posted():
    posted, statuses = [], []
    with tempfile.TemporaryDirectory() as d, clean_state(), \
         patched(s, _set_status=lambda slot, st="": statuses.append(st) or True), \
         patched(s.api, post_message_checked=lambda *a: posted.append(a) or "1"):
        reply = Path(d) / "reply.md"
        reply.write_text(s.NO_REPLY, encoding="utf-8")
        s._active_sessions.append(_slot(reply_file=reply, progress="status"))
        s._reap_nolock()
    assert not posted and statuses == [""], (posted, statuses)


def test_a_session_that_cannot_start_gives_its_slot_back():
    posted = []

    def boom(*a, **kw):
        raise PermissionError("prompt file locked")

    with clean_state(), patched(s, _spawn_into=boom), \
         patched(s.api, post_message=lambda *a: posted.append(a[2])):
        s._launch("U1", "D1", "1.0", "1.0", "p", resume_id=None, thread_key="1.0",
                  user_text="hi", is_dm=True)
        assert not s._active_sessions, "the slot leaked"
        assert "1.0" not in s._seen_timestamps, "the user must be able to send it again"
    assert posted and "send it again" in posted[0]


def test_the_instant_ack_is_claimed_once():
    ts = f"{time.time():.6f}"
    assert s.claim_ack(ts) is True
    assert s.claim_ack(ts) is False


def test_the_listener_leaves_dm_mentions_to_the_dm_handler():
    src = (Path(s.__file__).parent / "listener.py").read_text(encoding="utf-8")
    assert 'channel.startswith("D")' in src and src.count("claim_ack(current_ts)") == 2


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
