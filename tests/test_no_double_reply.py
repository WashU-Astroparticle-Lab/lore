"""A session that posts its own answer must not have it posted a second time.

On 2026-09-30 a lab-qa session posted its answer with `cli.slack post`, then ended
with "Message is confirmed in the thread. Here's the answer: ..." and the reaper posted
that as well. These are the two messages the lab actually received.

Run: python tests/run_all.py no_double_reply   (or: python tests/test_no_double_reply.py)
"""
from __future__ import annotations

import sys

from lab_agent.slack import sessions

POSTED_BY_SESSION = """Not quite — the dedicated amplitude sweep went from *AMP 0.0555 to 1.0* (not starting at 0.001). That was the sweep overlaid with the DAQ current sweep at 3.031 GHz.

AMP = 0.001 does show up in the data, but as a spot check during HEMT characterization (mapping out the 20 dB-per-decade scaling: 0.001 → −80 dBm, 0.01 → −60 dBm, 0.1 → −40 dBm). It was also flagged as a practical lower bound — the notes say don't go below 1e-3 for reliable measurements.

So the full calibration sweep range is *0.0555 – 1.0*, with 0.001 existing as a lower-limit sanity check, not a sweep endpoint.

Sources: 20260303_Amplitude_DAC_Sweep, 20260304_Power_Calibration_Sweep, 20250718 DAQ Current Sweep on Presto"""

FINAL_OUTPUT = """Message is confirmed in the thread. Here's the answer:

Not quite — the dedicated amplitude sweep went from **AMP 0.0555 to 1.0**, not 0.001. That was the sweep run at 3.031 GHz overlaid with the DAQ current sweep.

AMP = 0.001 does appear in the data, but as a **spot check** during HEMT characterization — mapping out the 20 dB-per-decade scaling (0.001 → −80 dBm, 0.01 → −60 dBm, 0.1 → −40 dBm). It was also noted as a practical lower bound: don't go below 1e-3 for reliable measurements.

So the calibration sweep range is **0.0555 – 1.0**, with 0.001 as a lower-limit sanity check, not a sweep endpoint. Sources: 20260303_Amplitude_DAC_Sweep, 20260304_Power_Calibration_Sweep, 20250718 DAQ Current Sweep on Presto."""

ACK = "Looking into it — answers usually take 1–5 minutes."
PROGRESS = "Starting the report pipeline: fetching the GitHub folder and 2 LabArchives pages."


def test_the_real_double_post_is_caught():
    assert sessions.duplicates_earlier_post(FINAL_OUTPUT, [ACK, POSTED_BY_SESSION])


def test_an_answer_the_session_did_not_post_is_still_delivered():
    assert not sessions.duplicates_earlier_post(FINAL_OUTPUT, [ACK, PROGRESS])
    assert not sessions.duplicates_earlier_post(FINAL_OUTPUT, [])


def test_a_different_answer_in_the_same_thread_is_not_suppressed():
    other = ("The lowest calibrated frequency is 2.35 GHz, the bottom of sub-band 1 "
             "(2.35–2.80 GHz), from power_calibration_20260227 run on 2026-03-03.")
    assert not sessions.duplicates_earlier_post(FINAL_OUTPUT, [other])


def test_short_outputs_are_never_judged_duplicates():
    assert not sessions.duplicates_earlier_post("Done — uploaded.", ["Done — uploaded."])


def test_if_the_thread_cannot_be_read_the_answer_is_posted_anyway():
    posted = []
    saved = (sessions._bot_posts_since, sessions.api.post_message)

    def unreadable(*a, **k):
        raise RuntimeError("conversations.replies: transport error")

    sessions._bot_posts_since = unreadable
    sessions.api.post_message = lambda ch, ts, text: posted.append(text)
    try:
        sessions._deliver_final_output(
            {"channel": "D1", "thread_ts": "1.0", "started_at": 0.0, "session_id": "x"},
            FINAL_OUTPUT)
    finally:
        sessions._bot_posts_since, sessions.api.post_message = saved
    assert posted == [FINAL_OUTPUT], "a failed check must not swallow the reply"


def test_a_duplicate_is_not_reposted_through_the_reaper_path():
    posted = []
    saved = (sessions._bot_posts_since, sessions.api.post_message)
    sessions._bot_posts_since = lambda *a: [ACK, POSTED_BY_SESSION]
    sessions.api.post_message = lambda ch, ts, text: posted.append(text)
    try:
        sessions._deliver_final_output(
            {"channel": "D1", "thread_ts": "1.0", "started_at": 0.0, "session_id": "x"},
            FINAL_OUTPUT)
    finally:
        sessions._bot_posts_since, sessions.api.post_message = saved
    assert posted == [], posted


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
