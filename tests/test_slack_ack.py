"""How LORE shows it is working on a request, without replying to every question.

A three-minute answer with no sign of life was reported as "the listener has paused";
the first fix, a "Looking into it..." reply on every question, was noise. Now: report
requests get "On it", a DM shows Slack's "is thinking..." status (no message, cleared
when the answer lands), and elsewhere a question still running after a minute gets
one short note. A quick answer gets nothing extra.

Run: python tests/run_all.py slack_ack   (or: python tests/test_slack_ack.py)
"""
from __future__ import annotations

import contextlib
import sys

from lab_agent.slack import sessions
from lab_agent.slack.sessions import acknowledgement, is_ack


class _Running:
    def poll(self):
        return None


@contextlib.contextmanager
def _fake_slack(status_ok: bool = True):
    """Run with a fake Slack: records statuses and posts, sends nothing."""
    calls = {"status": [], "posts": []}
    saved = (sessions.api.api_post, sessions.api.post_message, list(sessions._active_sessions))

    def api_post(endpoint, payload, timeout=30):
        assert endpoint == "assistant.threads.setStatus", endpoint
        if not status_ok:
            raise RuntimeError("assistant.threads.setStatus: not_allowed_token_type")
        calls["status"].append(payload["status"])
        return {"ok": True}

    sessions.api.api_post = api_post
    sessions.api.post_message = lambda ch, ts, text: calls["posts"].append(text)
    sessions._active_sessions.clear()
    try:
        yield calls
    finally:
        sessions.api.api_post, sessions.api.post_message = saved[0], saved[1]
        sessions._active_sessions[:] = saved[2]


def _session(is_dm: bool, text: str, started_at: float = 1000.0) -> dict:
    s = {"proc": _Running(), "channel": "D1", "thread_ts": "1.0", "started_at": started_at,
         "user_text": text, "is_dm": is_dm, "session_id": "x",
         "progress": sessions._initial_progress(is_dm, text)}
    sessions._active_sessions.append(s)
    return s


QUESTION = "what amplitude range did the power calibration sweep cover?"
REPORT = "write a report for https://github.com/x/y/tree/main/run"


def test_only_a_report_request_gets_an_immediate_message():
    assert acknowledgement(QUESTION) is None
    assert acknowledgement(REPORT) == "On it — starting now..."
    for text in ("thanks", "ok sounds good", "Perfect!"):
        assert is_ack(text), text


def test_a_dm_shows_the_thinking_status_and_keeps_it_alive_without_posting():
    with _fake_slack() as calls:
        _session(is_dm=True, text=QUESTION)
        sessions._progress_tick(now=1000.0)
        sessions._progress_tick(now=1030.0)            # too soon to refresh
        sessions._progress_tick(now=1000.0 + sessions.STATUS_REFRESH_S)
        sessions._progress_tick(now=1000.0 + 5 * 60)   # a five-minute answer
    assert calls["status"] == [sessions.THINKING_STATUS] * 3, calls
    assert calls["posts"] == [], "a DM with the status must not also get a note"


def test_a_dm_where_slack_refuses_the_status_falls_back_to_the_slow_note():
    with _fake_slack(status_ok=False) as calls:
        _session(is_dm=True, text=QUESTION)
        sessions._progress_tick(now=1000.0)
        sessions._progress_tick(now=1000.0 + sessions.SLOW_NOTE_AFTER_S)
    assert calls["posts"] == [sessions.SLOW_NOTE], calls


def test_a_channel_question_gets_one_note_only_if_it_is_slow():
    with _fake_slack() as calls:
        _session(is_dm=False, text=QUESTION)
        sessions._progress_tick(now=1030.0)
        assert calls["posts"] == [], "a quick answer must get nothing extra"
        for t in (1060.0, 1090.0, 1500.0):
            sessions._progress_tick(now=t)
    assert calls["posts"] == [sessions.SLOW_NOTE], "exactly one note, however long it runs"
    assert calls["status"] == [], "no status call outside a DM"


def test_a_report_in_a_channel_gets_no_extra_note():
    with _fake_slack() as calls:
        _session(is_dm=False, text=REPORT)
        sessions._progress_tick(now=5000.0)
    assert calls["posts"] == [], "a report already got 'On it'"


def test_a_skipped_duplicate_clears_the_dm_status():
    with _fake_slack() as calls:
        s = _session(is_dm=True, text=QUESTION)
        saved = sessions._bot_posts_since
        sessions._bot_posts_since = lambda *a: ["x" * 80]
        try:
            sessions._deliver_final_output(s, "x" * 80)
        finally:
            sessions._bot_posts_since = saved
    assert calls["status"] == [""], "nothing else would clear the status"


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
