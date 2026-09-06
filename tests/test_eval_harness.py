"""Tests for the eval harness (lab_agent.cli.eval): structural check + golden diff.

Run: python -m pytest tests/test_eval_harness.py   (or: python tests/test_eval_harness.py)
"""
import tempfile
from pathlib import Path

import lab_agent.cli.eval as ev

_GOOD_REPORT = """# [UNSIGNED] exp_x

**Experiment:** exp_x
**Report generated:** 2026-07-30

## Key Parameters
| Parameter | Value | Units | Notes | Source |
| --- | --- | --- | --- | --- |
| f0 | 5 | GHz |  | GitHub notebooks |

## Results
![a plot](github_images/p.png)
"""

_EXTRACTED = {
    "extracted_github.md": "## Key Parameters\n## Sweep Procedure\n## Numeric Results\n"
                           "## Figures\n## Cross-reference flags\n",
    "extracted_labarchives.md": "## Timeline\n## Lab Observations\n## Stated Goals\n"
                                 "## All Hyperlinks\n## Additional GitHub URLs\n"
                                 "## Attenuation Chain\n## Discrepancies\n## Figures\n"
                                 "## Cross-reference flags\n",
    "extracted_deps.md": "## `pkg`\n## Cross-reference flags\n",
    "connections.md": "## Power chain closure\n## Timeline correlations\n"
                      "## Lab observations vs. numeric results\n## Goal vs. executed map\n"
                      "## Dependency constants vs. notebook usage\n## Multi-source conflicts\n"
                      "## Figures needing cross-source context\n"
                      "## Additional GitHub URLs recommendation\n",
    "critique.md": "## Summary\nPASS\n",
    "metadata.json": '{"experiment_id": "exp_x"}',
}


def _good_dir(tmp: str) -> Path:
    d = Path(tmp)
    for name, text in _EXTRACTED.items():
        (d / name).write_text(text, encoding="utf-8")
    (d / "github_images").mkdir()
    (d / "github_images" / "p.png").write_bytes(b"x")
    (d / "[UNSIGNED] exp_x.md").write_text(_GOOD_REPORT, encoding="utf-8")
    # Stage A1: report-writer emits the Slack message alongside the report, and
    # the gate requires it — a complete run directory now includes one.
    (d / "slack_summary.md").write_text(
        "*exp_x* is in LabArchives.\n\n- One finding, consistent with the data\n",
        encoding="utf-8",
    )
    return d


def _errors(d: Path) -> list[str]:
    return [m for lvl, m in ev.check(d) if lvl == "ERROR"]


def test_check_clean():
    with tempfile.TemporaryDirectory() as tmp:
        assert _errors(_good_dir(tmp)) == []


def test_check_missing_section():
    with tempfile.TemporaryDirectory() as tmp:
        d = _good_dir(tmp)
        (d / "extracted_github.md").write_text("## Key Parameters\n", encoding="utf-8")
        assert any("Figures" in m for m in _errors(d))


def test_check_duplicate_key_parameters_table():
    with tempfile.TemporaryDirectory() as tmp:
        d = _good_dir(tmp)
        (d / "[UNSIGNED] exp_x.md").write_text(
            _GOOD_REPORT + "\n## Key Parameters\nagain\n", encoding="utf-8")
        assert any("Key Parameters" in m and "appears" in m for m in _errors(d))


def test_check_broken_image_wrong_folder():
    with tempfile.TemporaryDirectory() as tmp:
        d = _good_dir(tmp)
        (d / "[UNSIGNED] exp_x.md").write_text(
            _GOOD_REPORT.replace("github_images/p.png", "labarchives_images/p.png"),
            encoding="utf-8")
        assert any("image not found" in m for m in _errors(d))


def test_check_image_with_spaces_and_parens_resolves():
    with tempfile.TemporaryDirectory() as tmp:
        d = _good_dir(tmp)
        (d / "github_images" / "image (1).png").write_bytes(b"x")
        (d / "[UNSIGNED] exp_x.md").write_text(
            _GOOD_REPORT.replace("github_images/p.png", "github_images/image (1).png"),
            encoding="utf-8")
        assert not any("image not found" in m for m in _errors(d))


_DR_REPORT = """# [UNSIGNED] dr_20250201

**Date/window:** Feb 1 2025
**Report generated:** 2026-07-30

## Summary
The fridge was at base.

## Temperature Conditions
MXC 12 mK.

## System State Assessment
Good for measurements.
"""

_DR_EXTRACTED = ("## System State\n## Temperature Analysis\n## Pressure Analysis\n"
                 "## n_th calculation\n## Quasiparticle assessment\n## Anomaly flags\n"
                 "## Cross-reference flags\n")


def test_check_dr_only_report_not_false_flagged():
    # A DR-only report has no Key Parameters section and uses a Date/window header;
    # it must pass without false ERRORs (detected via extracted_dr.md + no github).
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "extracted_dr.md").write_text(_DR_EXTRACTED, encoding="utf-8")
        (d / "critique.md").write_text("## Summary\nPASS\n", encoding="utf-8")
        (d / "dr_conditions.md").write_text("| MXC | 12 mK |\n", encoding="utf-8")
        (d / "[UNSIGNED] dr_20250201.md").write_text(_DR_REPORT, encoding="utf-8")
        assert _errors(d) == [], _errors(d)


def test_diff_ignores_volatile_and_detects_change():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ev.GOLDEN_ROOT = root / "golden"          # redirect baseline into the temp dir
        d = root / "outputs" / "exp_y"
        d.mkdir(parents=True)
        (d / "notebooks.md").write_text("hello\n", encoding="utf-8")
        (d / "metadata.json").write_text(
            '{"experiment_id": "exp_y", "fetch_timings_sec": {"total": 1}}', encoding="utf-8")
        ev.update_golden(d)

        # Only the volatile timings changed → clean
        (d / "metadata.json").write_text(
            '{"experiment_id": "exp_y", "fetch_timings_sec": {"total": 999}}', encoding="utf-8")
        assert not [f for f in ev.diff(d) if f[0] == "ERROR"]

        # A real content change → ERROR
        (d / "notebooks.md").write_text("changed\n", encoding="utf-8")
        assert any(lvl == "ERROR" for lvl, _ in ev.diff(d))


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
