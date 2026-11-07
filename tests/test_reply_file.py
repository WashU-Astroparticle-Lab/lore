"""A session's reply is the file it writes, not whatever it printed on the way.

On 2026-10-20 three of the evening's replies reached the lab with the session's narration
around them: a lead-in ("I have enough context now. Let me compose the response. ...
here's a thorough reply: ---"), "Waiting for the github-analyst..." turns from background
agents, and a closing turn, woken by a background query that finished late, saying "The
response has already been posted to the thread. Nothing further needed." The narration
shapes below are verbatim from those replies; the answers are shortened.

Run: python tests/run_all.py reply_file   (or: python tests/test_reply_file.py)
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from lab_agent.slack import sessions

ANSWER = """OK so I looked through the channel and the TwoTone plots. The 3.73 GHz signal is most likely the qubit, landing low because the junction resistance came out higher than simulated.

**Why no Rabi:** the readout resonator is ~4.5 GHz away, so the dispersive shift is only a few MHz.

- Focus TwoTone on 3.6–3.8 GHz with higher control power
- Oscilloscope check on the pulse timing before the overnight run"""

LEAD_IN_AND_LATE_TURN = f"""I have enough context now. Let me compose the response.

Based on the image, Slack channel history, the knowledge graph, and the SQUAT/QPD lab history, here's a thorough reply:

---

{ANSWER}

The knowledge graph didn't find specific records of this current QPD run (it's too recent to be crawled), but the answer I already delivered is well-grounded in the Slack channel history. The KG output is consistent — no contradictions, just absence of the current data.

The response has already been posted to the thread. Nothing further needed."""

SUMMARY = """<@U1>

*presto_power_cal_oct2026_v2* — Presto amplitude↔power calibration updated across 0.1–9 GHz.

- Repeatability: 95.7% of low-band repeated settings agreed within 0.5 dB.

~8 min total (slowest phase: Phase D critique at ~3.3 min)"""

WAITING_TURNS = f"""All three Phase A agents running in parallel. Waiting for them to complete before moving to synthesis.

Waiting for the github-analyst and labarchives-analyst to finish.

Good — labarchives-analyst done. Still waiting for the github-analyst to finish.

Synthesis agent running. Waiting for it to complete before spawning report-writer.

Report-writer running in background. Waiting for it to complete before spawning the critic.

Critic running. Waiting for its verdict before attempting upload.

{SUMMARY}"""

UPLOADED_LEAD_IN = """The report uploaded successfully. Here's my final reply to the user:

---

Done! I created a simplified LabArchives page from your Word doc: **AI Agent / Presto Power Calibration Oct 2026**"""


def test_the_real_lead_in_and_late_turn_are_stripped():
    assert sessions.clean_stdout_reply(LEAD_IN_AND_LATE_TURN) == ANSWER


def test_the_real_waiting_turns_are_stripped():
    assert sessions.clean_stdout_reply(WAITING_TURNS) == SUMMARY


def test_the_real_upload_lead_in_is_stripped():
    assert sessions.clean_stdout_reply(UPLOADED_LEAD_IN).startswith("Done! I created")


def test_a_plain_answer_is_left_alone():
    assert sessions.clean_stdout_reply(ANSWER) == ANSWER
    ruled = "Two candidates:\n\n- BE260416\n- BE260417\n\n---\n\nBoth are on the 20260416 page."
    assert sessions.clean_stdout_reply(ruled) == ruled, "a rule not after a lead-in stays"


def test_output_that_is_all_narration_is_posted_whole():
    only = "Waiting for the critic to finish."
    assert sessions.clean_stdout_reply(only) == only, "a cluttered reply beats none"


def _slot(tmp: Path, reply: str | None, stdout: str) -> dict:
    log = tmp / "session_x_U1.log"
    log.write_text(f"=== Session x: user=U1 ===\n{stdout}\n", encoding="utf-8")
    reply_file = tmp / "reply_x.md"
    if reply is not None:
        reply_file.write_text(reply, encoding="utf-8")
    prompt_file = tmp / "_prompt_U1_x.txt"
    prompt_file.write_text("prompt", encoding="utf-8")
    return {"session_id": "x", "session_log": log, "reply_file": reply_file,
            "prompt_file": prompt_file}


def test_the_reply_file_beats_stdout():
    with tempfile.TemporaryDirectory() as d:
        s = _slot(Path(d), ANSWER + "\n", LEAD_IN_AND_LATE_TURN)
        assert sessions._reply_body(s) == ANSWER


def test_no_reply_file_falls_back_to_cleaned_stdout():
    with tempfile.TemporaryDirectory() as d:
        s = _slot(Path(d), None, LEAD_IN_AND_LATE_TURN)
        assert sessions._reply_body(s) == ANSWER
        s = _slot(Path(d), "  \n", LEAD_IN_AND_LATE_TURN)
        assert sessions._reply_body(s) == ANSWER, "an empty file is no reply"


class _Done:
    returncode = 0

    def poll(self):
        return 0


def test_the_reaper_posts_the_file_and_deletes_it():
    posted = []
    saved = (sessions._bot_posts_since, sessions.api.post_message_checked,
             list(sessions._active_sessions))
    with tempfile.TemporaryDirectory() as d:
        s = _slot(Path(d), ANSWER, LEAD_IN_AND_LATE_TURN)
        s.update(proc=_Done(), user="U1", channel="D1", thread_ts="1.0",
                 started_at=time.time(), timed_out=False, thread_key="1.0")
        sessions._bot_posts_since = lambda *a: []
        sessions.api.post_message_checked = lambda ch, ts, text: posted.append(text) or "9.9"
        sessions._active_sessions[:] = [s]
        try:
            sessions.reap_finished()
        finally:
            sessions._bot_posts_since, sessions.api.post_message_checked = saved[0], saved[1]
            sessions._active_sessions[:] = saved[2]
        assert posted == [ANSWER], posted
        assert not s["reply_file"].exists(), "reply files must not pile up"
        assert not s["prompt_file"].exists()


def test_leftover_reply_files_are_swept_at_startup():
    saved = sessions.REPLY_DIR
    with tempfile.TemporaryDirectory() as d:
        sessions.REPLY_DIR = Path(d)
        try:
            for name in ("reply_aaaa1111.md", "reply_bbbb2222.md", "progress_report.txt"):
                (Path(d) / name).write_text("x", encoding="utf-8")
            assert sessions.remove_stale_reply_files() == 2
            assert [p.name for p in Path(d).iterdir()] == ["progress_report.txt"]
        finally:
            sessions.REPLY_DIR = saved


def test_every_prompt_names_its_own_reply_file():
    launched = []
    saved = (sessions._launch, sessions.fetch_history, sessions.resumable_session)
    sessions._launch = lambda *a, **kw: launched.append((a[4], kw["session_id"]))
    sessions.fetch_history = lambda *a: ""
    try:
        sessions.resumable_session = lambda key: None
        sessions.spawn_claude("U1", "hi", "D1", None, "1.0", is_dm=True)
        sessions.resumable_session = lambda key: "prior-claude-session"
        sessions.spawn_claude("U1", "and the Qc?", "D1", "1.0", "2.0", is_dm=True)
    finally:
        sessions._launch, sessions.fetch_history, sessions.resumable_session = saved
    assert len(launched) == 2
    for prompt, session_id in launched:
        assert session_id, "the resumed path must pass its session id too"
        assert str(sessions.reply_path(session_id)) in prompt, prompt[-600:]


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
