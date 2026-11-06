"""The reaper must recover gracefully when a --resume of a prior Slack session fails.

Regression for the "reply in an existing thread -> exit 1 with a cryptic message" bug: a
failed resume should transparently re-run the SAME request as a FRESH session (not error),
while a plain (non-resume) failure still surfaces the error and must NOT retry (no loop).

Run: python tests/test_resume_fallback.py
"""
import time

import lab_agent.slack.sessions as s


class _FakeProc:
    returncode = 1
    def poll(self):
        return 1
    def kill(self):
        pass


def _slot(**over):
    base = dict(proc=_FakeProc(), user="U1", channel="C1", thread_ts="TH",
                started_at=time.time(), session_id="sid", session_log=None, prompt_file=None,
                timed_out=False, thread_key="TH", resume_of="old-claude-id", current_ts="111.22",
                orig_thread_ts=None, user_text="what is the mean Qc in BE260416?", is_dm=True)
    base.update(over)
    return base


def _run_with_stubs(slot, seen_ts):
    """Reap `slot` with side effects captured; returns (retries, posted, forgotten)."""
    orig = (s.api.post_message, s.forget_session, s.spawn_claude,
            list(s._active_sessions), set(s._seen_timestamps), list(s._pending_retries))
    retries, posted, forgotten = [], [], []
    try:
        s.api.post_message = lambda ch, tt, m: posted.append(m)
        s.forget_session = lambda tk: forgotten.append(tk)
        s.spawn_claude = lambda **kw: retries.append(kw)
        s._active_sessions[:] = [slot]
        s._seen_timestamps.clear()
        s._seen_timestamps.update(seen_ts)
        s._pending_retries.clear()
        s.reap_finished()
        return retries, posted, forgotten, slot in s._active_sessions, set(s._seen_timestamps)
    finally:
        (s.api.post_message, s.forget_session, s.spawn_claude) = orig[0], orig[1], orig[2]
        s._active_sessions[:] = orig[3]
        s._seen_timestamps.clear(); s._seen_timestamps.update(orig[4])
        s._pending_retries[:] = orig[5]


def test_failed_resume_retries_fresh():
    retries, posted, forgotten, still_active, seen = _run_with_stubs(_slot(), {"111.22"})
    assert forgotten == ["TH"], "resume mapping must be dropped"
    assert "111.22" not in seen, "dedup ts must be cleared so the retry is not dropped"
    assert not still_active, "failed slot must be removed"
    assert len(retries) == 1, retries
    assert retries[0] == {"user": "U1", "text": "what is the mean Qc in BE260416?",
                          "channel": "C1", "thread_ts": None, "current_ts": "111.22",
                          "is_dm": True}, retries[0]
    assert not posted, "a recovered resume should not post a cryptic error"


def test_non_resume_failure_does_not_retry():
    retries, posted, _f, _a, _s = _run_with_stubs(_slot(resume_of=None), {"111.22"})
    assert not retries, "a non-resume failure must not spawn a retry (no loop)"
    assert posted and "ended unexpectedly" in posted[0], posted


def test_transcript_folder_matches_claude_codes_naming():
    # The real folders on the lab PC, next to the paths they belong to.
    assert (s.project_dir_name(r"C:\Users\LC Control\Desktop\Lab-Agent-main\LORE")
            == "C--Users-LC-Control-Desktop-Lab-Agent-main-LORE")
    assert s.project_dir_name(r"C:\Users\LC Control") == "C--Users-LC-Control"
    assert s.project_dir_name("/home/lab/lab_agent.v2/LORE") == "-home-lab-lab-agent-v2-LORE"


def test_a_thread_with_a_transcript_is_resumed():
    # Replacing only "/" and "." pointed the lookup at LORE\<id>.jsonl on Windows, so this
    # returned None for every thread and all 57 sessions on the lab PC started fresh.
    import json
    import os
    import re
    import tempfile
    from pathlib import Path

    # Built here with Claude Code's own rule, not with project_dir_name: under the old
    # rule the folder below collapsed onto the real LORE checkout.
    name = re.sub(r"[^A-Za-z0-9]", "-", str(s.PROJECT_ROOT))
    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
    saved_map = s.SESSION_MAP_PATH
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.environ["USERPROFILE"] = os.environ["HOME"] = tmp
            folder = Path(tmp) / ".claude" / "projects" / name
            assert str(folder).startswith(tmp), folder
            folder.mkdir(parents=True)
            (folder / "sess-1.jsonl").write_text("{}\n", encoding="utf-8")
            s.SESSION_MAP_PATH = Path(tmp) / "session_map.json"
            s.SESSION_MAP_PATH.write_text(json.dumps({
                "111.22": {"claude_session_id": "sess-1", "updated_at": 1},
                "333.44": {"claude_session_id": "sess-gone", "updated_at": 1},
            }), encoding="utf-8")
            assert s.resumable_session("111.22") == "sess-1", "a thread with a transcript must resume"
            assert s.resumable_session("333.44") is None, "a missing transcript must start fresh"
            assert s.resumable_session("999.99") is None
        finally:
            s.SESSION_MAP_PATH = saved_map
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    import sys

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
