"""Stage A1 — the structural gate requires a Slack summary alongside the report.

The report is checked by 9 critic items and a structural gate; the Slack message
the lab actually reads was checked by nothing, and that is where both factual
defects of the 2026-08 review occurred. report-writer now emits slack_summary.md;
this test covers the deterministic half of the gate (presence and shape). The
number/hedging comparison against the report is critic item 10.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab_agent.cli import eval as ev

PASSED = 0


def check(label: str, cond: bool) -> None:
    global PASSED
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    PASSED += 1
    print(f"  ok  {label}")


REPORT = """# [UNSIGNED] demo_experiment

**Experiment:** demo_experiment
**Report generated:** 2026-09-05

---

## Key Parameters

| Parameter | Value | Units | Notes | Source |
|---|---|---|---|---|
| Frequency | 6.9 | GHz | tone | extracted_github.md |

---

## Results

The Presto tone read -29.84 dBm when -30 dBm was commanded.
"""

SUMMARY = """*demo_experiment* is in LabArchives.

- Presto read -29.84 dBm at a commanded -30 dBm, consistent with the VNA
LabArchives: AI Agent / [UNSIGNED] demo_experiment
"""


SOURCES = (
    "# Sources for demo_experiment\n\n"
    "| Value or claim | Where in report | Extracted file | Source line |\n"
    "|---|---|---|---|\n"
    '| -29.84 dBm | Main Result | extracted_github.md | "measured -29.84 dBm" |\n'
)

# The brief template (Stage B default): no ToC, no Key Parameters table, no
# Citations — headings state the finding instead.
BRIEF_REPORT = """# [UNSIGNED] demo_experiment

**Experiment:** demo_experiment
**Report generated:** 2026-09-05

---

## What We Did

We compared the Presto against a VNA on the spectrum analyser.

---

## Main Result: Presto and the VNA Agree

The Presto tone read -29.84 dBm when -30 dBm was commanded.

**Bottom line:** either source is trustworthy for setup work.
"""


def _mkrun(summary: str | None, report: str = REPORT, sources: str | None = SOURCES) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "[UNSIGNED] demo_experiment.md").write_text(report, encoding="utf-8")
    if summary is not None:
        (d / "slack_summary.md").write_text(summary, encoding="utf-8")
    if sources is not None:
        (d / "report_sources.md").write_text(sources, encoding="utf-8")
    return d


def _findings(out_dir: Path) -> tuple[list[str], list[str]]:
    """Run the gate, returning (error messages, warning messages)."""
    findings = ev.check(out_dir)
    errors = [msg for level, msg in findings if level == "ERROR"]
    warnings = [msg for level, msg in findings if level == "WARN"]
    return errors, warnings


def test_missing_summary_is_an_error() -> None:
    errors, _ = _findings(_mkrun(None))
    check(
        "a missing slack_summary.md is an ERROR",
        any("slack_summary" in e for e in errors),
    )

    errors, _ = _findings(_mkrun("   \n  "))
    check(
        "an empty slack_summary.md is an ERROR",
        any("slack_summary" in e for e in errors),
    )


def test_good_summary_passes() -> None:
    errors, warnings = _findings(_mkrun(SUMMARY))
    check("a normal summary raises no slack_summary error", not any("slack_summary" in e for e in errors))
    check("a normal summary raises no slack_summary warning", not any("slack_summary" in w for w in warnings))


def test_shape_warnings() -> None:
    _, warnings = _findings(_mkrun(SUMMARY + '\n<table><tr><td>x</td></tr></table>\n'))
    check("HTML in the summary warns", any("HTML" in w for w in warnings))

    _, warnings = _findings(_mkrun(SUMMARY + "\nfiller. " * 500))
    check("an over-long summary warns", any("too long" in w for w in warnings))


def test_brief_template_is_accepted() -> None:
    """Stage B: a report with no Key Parameters table must pass the gate.

    The gate used to ERROR without that section, so it was silently re-added
    twice against an explicit instruction to drop it.
    """
    errors, _ = _findings(_mkrun(SUMMARY, report=BRIEF_REPORT))
    check("a brief report raises no Key Parameters error",
          not any("Key Parameters" in e for e in errors))
    check("a brief report raises no errors at all", errors == [])


def test_sources_sidecar_required() -> None:
    errors, _ = _findings(_mkrun(SUMMARY, report=BRIEF_REPORT, sources=None))
    check("a missing report_sources.md is an ERROR",
          any("report_sources" in e for e in errors))

    header_only = (
        "| Value or claim | Where in report |\n|---|---|\n"
    )
    _, warnings = _findings(_mkrun(SUMMARY, report=BRIEF_REPORT, sources=header_only))
    check("a sidecar with no value rows warns",
          any("report_sources" in w for w in warnings))


def test_duplicate_key_parameters_still_caught() -> None:
    doubled = REPORT + "\n## Key Parameters\n\n| a | b |\n|---|---|\n"
    errors, _ = _findings(_mkrun(SUMMARY, report=doubled))
    check("two Key Parameters sections are still an ERROR",
          any("Key Parameters" in e and "2x" in e for e in errors))


if __name__ == "__main__":
    test_missing_summary_is_an_error()
    test_good_summary_passes()
    test_shape_warnings()
    test_brief_template_is_accepted()
    test_sources_sidecar_required()
    test_duplicate_key_parameters_still_caught()
    print(f"\ntest_slack_summary_gate: {PASSED} checks passed")
