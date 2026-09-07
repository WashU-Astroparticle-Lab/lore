"""A report's Slack summary must come from the gated file, not from prose.

On 2026-09-06 a revision request produced, 25 seconds later, a freehand
three-bullet summary ending in "LabArchives: AI Agent / 20250904 Standalone Warm
Amp Noise Digest" — while the report on disk was untouched, the critic had not
re-run, and nothing had been uploaded. The message looked like a completed
revision. `post-summary` removes the freehand path: it takes a --run, not text.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab_agent.cli import slack as cli

PASSED = 0


def check(label: str, cond: bool) -> None:
    global PASSED
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    PASSED += 1
    print(f"  ok  {label}")


def _run(summary: str, verdict: str = "passed", upload: bool = False) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "slack_summary.md").write_text(summary, encoding="utf-8")
    (d / "critique.md").write_text(f"## Summary\n\n{verdict}\n", encoding="utf-8")
    meta = {"experiment_id": d.name}
    if upload:
        meta["labarchives_upload"] = {"folder": "AI Agent", "page_title": "[UNSIGNED] x"}
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def _exit_code(**opts) -> int:
    """Run cmd_post_summary and capture the SystemExit code (0 = would post)."""
    try:
        cli.cmd_post_summary(opts)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_there_is_no_way_to_pass_your_own_words() -> None:
    src = Path(cli.__file__).read_text(encoding="utf-8")
    body = src.split("def cmd_post_summary")[1].split("\ndef ")[0]
    check("cmd_post_summary never reads a --text-file or --text option",
          "text_file" not in body and '"text"' not in body)
    code = _exit_code(channel="C1", text_file="anything.txt")
    check("without --run it refuses", code == 1)

    code = _exit_code(channel="C1", run=str(Path(tempfile.mkdtemp()) / "nope"))
    check("a non-existent run refuses", code == 1)

    d = Path(tempfile.mkdtemp())
    (d / "critique.md").write_text("## Summary\n\npassed\n", encoding="utf-8")
    check("a run with no slack_summary.md refuses", _exit_code(channel="C1", run=str(d)) == 2)


def test_a_failed_critique_cannot_be_announced() -> None:
    d = _run("- a finding\n", verdict="gaps_found: 1")
    check("gaps_found blocks the announcement (exit 5)",
          _exit_code(channel="C1", run=str(d)) == 5)

    d = _run("- a finding\n", verdict="expert_needed: which tone?")
    check("expert_needed blocks too", _exit_code(channel="C1", run=str(d)) == 5)

    d = Path(tempfile.mkdtemp())
    (d / "slack_summary.md").write_text("- a finding\n", encoding="utf-8")
    check("a run with no critique at all blocks",
          _exit_code(channel="C1", run=str(d)) == 5)


def test_a_cited_location_must_be_real() -> None:
    """The exact shape of the 23:38 message: a LabArchives line, no upload."""
    d = _run("- a finding\n\nLabArchives: AI Agent / [UNSIGNED] x\n", upload=False)
    check("citing LabArchives with no upload record blocks",
          _exit_code(channel="C1", run=str(d)) == 5)

    d = _run("- a finding\n\nLabArchives: <filled in on upload>\n", upload=True)
    check("an unfilled upload placeholder blocks",
          _exit_code(channel="C1", run=str(d)) == 5)

    # A summary that makes no location claim needs no upload record. Stub the
    # API: a unit test must never reach Slack (or LabArchives — a "test" that
    # did left 27 pages in the lab notebook).
    d = _run("- a finding with no location claim\n", upload=False)
    sent: dict = {}
    real_post, real_exists = cli.api.post_message_checked, cli.api.message_exists
    cli.api.post_message_checked = lambda ch, th, text: sent.update(
        channel=ch, thread=th, text=text) or "1.23"
    cli.api.message_exists = lambda ch, ts: True
    try:
        code = _exit_code(channel="C1", run=str(d), tag="U9", timing="41 s")
    finally:
        cli.api.post_message_checked, cli.api.message_exists = real_post, real_exists
    check("no location claim, no upload needed — it posts", code == 0)
    check("the summary text goes out verbatim",
          "a finding with no location claim" in sent["text"])
    check("the user tag is added", sent["text"].startswith("<@U9>"))
    check("the timing line is added", sent["text"].rstrip().endswith("_Pipeline: 41 s_"))


def test_registered_as_a_command() -> None:
    check("post-summary is a real subcommand", "post-summary" in cli.COMMANDS)
    check("it maps to cmd_post_summary", cli.COMMANDS["post-summary"] is cli.cmd_post_summary)


if __name__ == "__main__":
    test_there_is_no_way_to_pass_your_own_words()
    test_a_failed_critique_cannot_be_announced()
    test_a_cited_location_must_be_real()
    test_registered_as_a_command()
    print(f"\ntest_post_summary: {PASSED} checks passed")
