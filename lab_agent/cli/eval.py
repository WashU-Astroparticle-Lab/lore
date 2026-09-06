"""Layered eval harness for LORE experiment output directories.

LORE produces two kinds of artifacts, and each needs a different check:

  * ``check`` — structural invariants on the LLM-produced artifacts
    (``extracted_*.md``, the ``[UNSIGNED]`` report, ``critique.md``). No baseline,
    runs on any output dir. LLM wording varies run-to-run, so we validate
    *structure* (required sections, table appears once, embedded images resolve),
    not exact bytes.

  * ``diff`` — byte/JSON comparison of the DETERMINISTIC Python-produced artifacts
    (``notebooks.md``, ``data_summaries.md``, ``dependencies.md``, ``labarchives.md``,
    ``metadata.json``) against a golden snapshot in ``tests/golden/<exp>/``.
    ``--update`` (re-)creates the baseline. Volatile metadata (fetch timings) is
    ignored so an unchanged run diffs clean.

Component-level fixture tests for the pure functions live under ``tests/`` and run
with pytest; they complement this directory-level harness.

Usage:
  python -m lab_agent.cli.eval check outputs/<exp>
  python -m lab_agent.cli.eval diff  outputs/<exp> [--update]
  python -m lab_agent.cli.eval all   outputs/<exp>
"""
from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path

from ..config import PROJECT_ROOT

GOLDEN_ROOT = PROJECT_ROOT / "tests" / "golden"

# Deterministic artifacts written by run.py — safe to byte/JSON-diff against golden.
DETERMINISTIC_FILES = [
    "notebooks.md",
    "data_summaries.md",
    "dependencies.md",
    "labarchives.md",
    "metadata.json",
]

# metadata.json keys that change every run and must be ignored by the diff.
_VOLATILE_METADATA_KEYS = {"fetch_timings_sec", "run_timestamp"}

# Required ## sections per extracted/synthesis file (from .claude/agents/*.md schemas).
_REQUIRED_SECTIONS = {
    "extracted_github.md": [
        "Key Parameters", "Sweep Procedure", "Numeric Results", "Figures",
        "Cross-reference flags",
    ],
    "extracted_labarchives.md": [
        "Timeline", "Lab Observations", "Stated Goals", "All Hyperlinks",
        "Additional GitHub URLs", "Attenuation Chain", "Discrepancies", "Figures",
        "Cross-reference flags",
    ],
    "extracted_deps.md": ["Cross-reference flags"],
    "extracted_dr.md": [
        "System State", "Temperature Analysis", "Pressure Analysis",
        "n_th calculation", "Quasiparticle assessment", "Anomaly flags",
        "Cross-reference flags",
    ],
    "connections.md": [
        "Power chain closure", "Timeline correlations",
        "Lab observations vs. numeric results", "Goal vs. executed map",
        "Dependency constants vs. notebook usage", "Multi-source conflicts",
        "Figures needing cross-source context", "Additional GitHub URLs recommendation",
    ],
}

# Files that should exist for a full experiment run (report matched by glob).
_EXPECTED_FILES = [
    "metadata.json", "extracted_github.md", "extracted_labarchives.md",
    "extracted_deps.md", "connections.md", "critique.md",
]

Finding = tuple  # (level: "ERROR"|"WARN", message: str)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _heading_names(text: str) -> list[str]:
    """Return the trimmed text of every ATX heading line (##, ###, ...)."""
    return [m.group(1).strip() for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", text, re.M)]


def _missing_sections(text: str, required: list[str]) -> list[str]:
    headings = " \n ".join(_heading_names(text)).lower()
    return [s for s in required if s.lower() not in headings]


# ---------------------------------------------------------------------------
# check — structural invariants (LLM artifacts)
# ---------------------------------------------------------------------------

def check(out_dir: Path) -> list[Finding]:
    findings: list[Finding] = []

    def err(msg: str) -> None:
        findings.append(("ERROR", msg))

    def warn(msg: str) -> None:
        findings.append(("WARN", msg))

    # DR-only reports (from the DR-only workflow) have a different structure: no
    # Key Parameters section, a "Date/window" header instead of "Experiment", and a
    # smaller file set. Detect that mode so its reports aren't false-flagged.
    dr_only = out_dir.name.startswith("dr_") or (
        (out_dir / "extracted_dr.md").exists()
        and not (out_dir / "extracted_github.md").exists()
    )

    # 1. Expected files present
    expected = (["dr_conditions.md", "extracted_dr.md", "critique.md"]
                if dr_only else _EXPECTED_FILES)
    for name in expected:
        if not (out_dir / name).exists():
            warn(f"expected file missing: {name}")

    # 2. Required ## sections in extracted/synthesis files
    for name, required in _REQUIRED_SECTIONS.items():
        path = out_dir / name
        text = _read(path)
        if text is None:
            continue  # absence handled above / file is optional (e.g. dr)
        for sec in _missing_sections(text, required):
            err(f"{name}: missing required section '## {sec}'")

    # 3. critique.md has a PASS/FAIL (or 4-outcome) summary
    crit = _read(out_dir / "critique.md")
    if crit is not None:
        if not re.search(r"##\s*Summary", crit, re.I):
            warn("critique.md: no '## Summary' line")
        elif not re.search(r"\b(PASS|FAIL|passed|gaps_found|expert_needed|human_needed)\b", crit):
            warn("critique.md: Summary states no recognizable verdict")

    # 4. Report ([UNSIGNED] *.md) invariants
    reports = sorted(out_dir.glob("[[]UNSIGNED[]] *.md"))
    if not reports:
        warn("no [UNSIGNED] report found")
    else:
        report = reports[-1]
        text = _read(report) or ""
        if not text.lstrip().startswith("# [UNSIGNED]"):
            err(f"{report.name}: does not start with '# [UNSIGNED]' title")
        markers = (("**Date/window:**", "**Report generated:**") if dr_only
                   else ("**Experiment:**", "**Report generated:**"))
        for marker in markers:
            if marker not in text:
                warn(f"{report.name}: missing metadata marker {marker}")
        if not dr_only:
            # Key Parameters section appears exactly once, with a Source column
            # (report_style_guide §4). DR-only reports have no such table.
            kp = [h for h in _heading_names(text) if "key parameters" in h.lower()]
            if len(kp) == 0:
                err(f"{report.name}: no Key Parameters section")
            elif len(kp) > 1:
                err(f"{report.name}: Key Parameters section appears {len(kp)}x (must be once)")
            if "| source" not in text.lower():
                warn(f"{report.name}: Key Parameters table appears to lack a 'Source' column")
        # The Slack summary is posted to the lab verbatim and is the only part
        # most people read, so it must exist and be reviewable (Stage A1). The
        # critic checks its numbers and hedging against the report; here we only
        # confirm it is present and plausible.
        if not dr_only:
            summary_path = out_dir / "slack_summary.md"
            summary_text = _read(summary_path) or ""
            if not summary_text.strip():
                err("slack_summary.md missing or empty (report-writer must emit it)")
            else:
                if "<img" in summary_text or "<table" in summary_text:
                    warn("slack_summary.md contains HTML — Slack renders it literally")
                if len(summary_text) > 3000:
                    warn(f"slack_summary.md is {len(summary_text)} chars — too long for a Slack post")

        # Every embedded image resolves to a file in out_dir (catches wrong-folder-prefix bug).
        # Capture to the last ')' on the line so filenames containing '(', ')' or spaces
        # (e.g. 'image (1).png') survive; then strip an optional "title".
        for m in re.finditer(r"!\[[^\]]*\]\((.+)\)\s*$", text, re.M):
            inner = m.group(1).strip().strip("<>")
            title = re.match(r'^(.*?)\s+["\'][^"\']*["\']$', inner)
            rel = (title.group(1) if title else inner).strip()
            if rel.startswith(("http://", "https://", "data:")):
                continue
            if not (out_dir / rel).exists():
                err(f"{report.name}: embedded image not found: {rel}")

    return findings


# ---------------------------------------------------------------------------
# diff — golden comparison (deterministic artifacts)
# ---------------------------------------------------------------------------

def _normalize_metadata(text: str) -> str:
    try:
        obj = json.loads(text)
    except Exception:
        return text
    for k in _VOLATILE_METADATA_KEYS:
        obj.pop(k, None)
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def _golden_dir(out_dir: Path) -> Path:
    return GOLDEN_ROOT / out_dir.name


def update_golden(out_dir: Path) -> list[Finding]:
    gdir = _golden_dir(out_dir)
    gdir.mkdir(parents=True, exist_ok=True)
    written = 0
    for name in DETERMINISTIC_FILES:
        src = out_dir / name
        text = _read(src)
        if text is None:
            continue
        if name == "metadata.json":
            text = _normalize_metadata(text)
        (gdir / name).write_text(text, encoding="utf-8")
        written += 1
    return [("WARN", f"golden baseline written for {out_dir.name} ({written} files) at {gdir}")]


def diff(out_dir: Path) -> list[Finding]:
    findings: list[Finding] = []
    gdir = _golden_dir(out_dir)
    if not gdir.is_dir():
        return [("WARN", f"no golden baseline for {out_dir.name}; "
                          f"run:  python -m lab_agent.cli.eval diff {out_dir} --update")]
    for name in DETERMINISTIC_FILES:
        golden = _read(gdir / name)
        if golden is None:
            continue  # not part of this experiment's baseline
        current = _read(out_dir / name)
        if current is None:
            findings.append(("ERROR", f"{name}: present in golden but missing from run"))
            continue
        if name == "metadata.json":
            current = _normalize_metadata(current)
        if current != golden:
            delta = list(difflib.unified_diff(
                golden.splitlines(), current.splitlines(),
                fromfile=f"golden/{name}", tofile=f"current/{name}", lineterm="", n=1,
            ))
            preview = "\n    ".join(delta[:12])
            findings.append(("ERROR", f"{name}: differs from golden\n    {preview}"))
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print(findings: list[Finding]) -> int:
    errors = sum(1 for lvl, _ in findings if lvl == "ERROR")
    if not findings:
        print("  OK - no findings")
    for lvl, msg in findings:
        print(f"  [{lvl}] {msg}")
    return errors


def main() -> None:
    # Diff previews echo report content (µ, −, —); avoid a cp1252 console crash.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in {"check", "diff", "all"}:
        print(__doc__)
        sys.exit(2)
    mode, out_dir = args[0], Path(args[1])
    update = "--update" in args[2:]
    if not out_dir.is_dir():
        print(f"Not a directory: {out_dir}")
        sys.exit(2)

    errors = 0
    if mode in ("check", "all"):
        print(f"[check] {out_dir}")
        errors += _print(check(out_dir))
    if mode == "diff" and update:
        print(f"[diff --update] {out_dir}")
        _print(update_golden(out_dir))
    elif mode in ("diff", "all"):
        print(f"[diff] {out_dir}")
        errors += _print(diff(out_dir))

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
