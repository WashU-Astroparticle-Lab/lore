"""The upload must refuse a report the critic failed.

Phase D's verdict was enforced only by prose in the skill, and on the
2026-09-06 warm-amp run the orchestrator wrote `gaps_found: 1` at 23:16 and
uploaded at 23:17 — no revision pass, no second critique. These tests pin the
parser against the real critique.md wording (the outcome was inside backticks,
on line 187, with the checklist's own vocabulary quoted all through the body)
and pin the footer rewrite that shipped the wrong page name to the lab.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab_agent.cli.upload import _parse_args, fix_location_footer
from lab_agent.critique import blocks_upload, explain, read_verdict

PASSED = 0


def check(label: str, cond: bool) -> None:
    global PASSED
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    PASSED += 1
    print(f"  ok  {label}")


def _dir(critique: str | None) -> Path:
    d = Path(tempfile.mkdtemp())
    if critique is not None:
        (d / "critique.md").write_text(critique, encoding="utf-8")
    return d


# The shape of the real file: PASS/FAIL words and quoted checklist vocabulary
# in the body, the actual outcome in backticks under a `## Summary` heading.
REAL = """# Critique

## Item 1 — Every numeric value is sourced

**FAIL**

The value `~20 dB` has no row in provenance.md. Fix: add one row.
Note: item 3 asks whether "confirms" is used; it is not. All other values
checked and confirmed sourced.

## Item 10 — Slack summary matches the report

**PASS**

No claim appears that is absent from the report.

---

## Summary

`gaps_found: 1`

**Item 1** fails: the value `~20 dB` appears in the report body.
"""


def test_real_critique_is_read_as_a_block() -> None:
    v = read_verdict(_dir(REAL))
    check("the real critique reads as gaps_found", v.outcome == "gaps_found")
    check("the failing item number survives", v.detail == "1")
    check("backticks are stripped, not parsed as part of the outcome", "`" not in v.detail)
    check("gaps_found blocks the upload", blocks_upload(v) is True)
    msg = explain(v)
    check("the message says what to do instead", "Revision mode" in msg)
    check("the message names the override", "--force" in msg)
    # The body says PASS, FAIL and "confirmed sourced" — none may be mistaken
    # for the verdict.
    check("prose in the body is not mistaken for a verdict", v.outcome == "gaps_found")


def test_passed_is_the_only_green_light() -> None:
    v = read_verdict(_dir("## Summary\n\npassed\n"))
    check("passed does not block", blocks_upload(v) is False and v.clean)

    for outcome in ("expert_needed: which tone is right?", "human_needed: report unreadable"):
        v = read_verdict(_dir(f"## Summary\n\n{outcome}\n"))
        check(f"{outcome.split(':')[0]} blocks", blocks_upload(v) is True)
        check(f"{outcome.split(':')[0]} tells the agent not to upload",
              "Do not upload" in explain(v))


def test_missing_and_unreadable_block() -> None:
    """An uncritiqued report reached the notebook once; absence is not consent."""
    v = read_verdict(_dir(None))
    check("no critique.md blocks", v.outcome == "missing" and blocks_upload(v))
    check("the message says it was never critiqued", "never" in explain(v))

    v = read_verdict(_dir("# Critique\n\nEverything looks fine to me.\n"))
    check("a critique with no outcome line blocks",
          v.outcome == "unreadable" and blocks_upload(v))
    check("the message lists the valid outcomes", "gaps_found" in explain(v))


def test_last_outcome_wins() -> None:
    """A revision pass appends; the final verdict is the one that counts."""
    two = ("## Summary\n\n`gaps_found: 1`\n\n"
           "## Summary (after revision)\n\n`passed`\n")
    check("the later verdict wins", read_verdict(_dir(two)).outcome == "passed")


def test_legacy_critiques_block_but_never_pass() -> None:
    """Older critiques end `FAIL (1)` or `FAIL - Items 1, 3 and 7 failed.`

    Both forms are on disk. They must block — but a legacy `PASS` must NOT
    authorise an upload: per-item PASS lines look exactly like a summary
    verdict, so trusting the last one would wave a failed report through.
    """
    v = read_verdict(_dir("## Summary\n\nFAIL (1)\n"))
    check("legacy FAIL (1) blocks", v.outcome == "gaps_found" and blocks_upload(v))
    check("the detail flags the old format", "legacy" in v.detail)

    v = read_verdict(_dir("## Summary\n\nFAIL - Items 1, 3, and 7 failed.\n"))
    check("legacy prose FAIL blocks", v.outcome == "gaps_found" and blocks_upload(v))

    # A per-item PASS is not a verdict.
    v = read_verdict(_dir("## Item 1\n\n**PASS**\n\n## Item 2\n\n**PASS**\n"))
    check("a critique of only per-item PASSes still blocks", blocks_upload(v))
    check("and is reported as unreadable, not passed", v.outcome == "unreadable")

    # A modern verdict always wins over legacy wording in the body.
    both = "## Item 1\n\n**FAIL**\n\n## Summary\n\npassed\n"
    check("an explicit modern verdict wins", read_verdict(_dir(both)).outcome == "passed")


def test_force_flag_parses() -> None:
    _, opts = _parse_args(["outputs/x"])
    check("force is off by default", opts["force"] is False)
    _, opts = _parse_args(["outputs/x", "--force", "--new-page"])
    check("--force parses alongside --new-page",
          opts["force"] is True and opts["new_page"] is True)


def test_footer_is_rewritten_not_appended() -> None:
    d = Path(tempfile.mkdtemp())
    report = d / "[UNSIGNED] exp.md"
    # What the writer actually shipped: the source notes page, named twice.
    report.write_text(
        "# [UNSIGNED] exp\n\nBody text.\n\n"
        "LabArchives: 20250904 Standalone Warm Amp Noise Digest / "
        "20250904 Standalone Warm Amp Noise Digest\n",
        encoding="utf-8")
    summary = d / "slack_summary.md"
    summary.write_text("- a finding\n\nLabArchives: <filled in on upload>\n", encoding="utf-8")

    changed = fix_location_footer([report, summary, d / "nope.md"],
                                  "AI Agent", "[UNSIGNED] 20250904_standalone_warm_amp")
    check("both existing files were rewritten", len(changed) == 2)
    for f in (report, summary):
        text = f.read_text(encoding="utf-8")
        check(f"{f.name} names the upload folder and page",
              "LabArchives: AI Agent / [UNSIGNED] 20250904_standalone_warm_amp" in text)
        check(f"{f.name} has exactly one LabArchives line",
              sum(1 for line in text.splitlines() if line.startswith("LabArchives:")) == 1)
    check("the source page name is gone from the report",
          "Noise Digest / 20250904" not in report.read_text(encoding="utf-8"))
    check("body content is untouched", "Body text." in report.read_text(encoding="utf-8"))
    check("a missing file is skipped, not created", not (d / "nope.md").exists())

    # Idempotent: uploading twice must not stack footers.
    fix_location_footer([report], "AI Agent", "[UNSIGNED] 20250904_standalone_warm_amp")
    check("re-running changes nothing",
          sum(1 for line in report.read_text(encoding="utf-8").splitlines()
              if line.startswith("LabArchives:")) == 1)


if __name__ == "__main__":
    test_real_critique_is_read_as_a_block()
    test_passed_is_the_only_green_light()
    test_missing_and_unreadable_block()
    test_last_outcome_wins()
    test_legacy_critiques_block_but_never_pass()
    test_force_flag_parses()
    test_footer_is_rewritten_not_appended()
    print(f"\ntest_critique_gate: {PASSED} checks passed")
