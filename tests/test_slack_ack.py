"""Every request is acknowledged the moment it arrives.

Only report requests used to be. A question got nothing until its answer, and one
that took three minutes on 2026-09-30 was reported as "the listener has paused".

Run: python tests/run_all.py slack_ack   (or: python tests/test_slack_ack.py)
"""
from __future__ import annotations

import sys

from lab_agent.slack.sessions import acknowledgement, is_ack


def test_a_question_is_told_how_long_it_takes():
    msg = acknowledgement("what is the lowest frequency Axel calibrated the power at?")
    assert msg.startswith("Looking into it") and "minutes" in msg, msg


def test_a_report_request_keeps_its_own_acknowledgement():
    assert acknowledgement("write a report for https://github.com/x/y/tree/main/run") \
        == "On it — starting now..."
    assert acknowledgement("give me a DR report for Sept 18") == "On it — starting now..."


def test_thanks_still_gets_no_reply_at_all():
    # The handlers return before acknowledging when is_ack() is true.
    for text in ("thanks", "ok sounds good", "Perfect!"):
        assert is_ack(text), text


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
