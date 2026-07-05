"""
run_phase_d.py — Phase D critique via direct Anthropic API.

Reads the [UNSIGNED] report, all extracted_*.md files, and connections.md.
Writes critique.md with PASS/FAIL results for each checklist item.
Uses ANTHROPIC_API_KEY from .env (not the Claude Code Pro plan).

Exit code: 0 if all items PASS, 1 if any FAIL (so the orchestrator knows
whether to trigger a revision pass).

Usage:
    python run_phase_d.py outputs/<experiment_id>
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent.resolve()
load_dotenv(_PROJECT_ROOT / ".env", override=True)

import anthropic

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 4096

_SYSTEM = """\
You are the Critic Agent. Run the following fixed checklist against the experiment \
report. For each item write PASS or FAIL. For every FAIL, quote the exact sentence \
from the report that fails and explain why.

Checklist:
1. Every numeric value in the report's Key Parameters table appears with the same \
number in extracted_github.md or extracted_deps.md.
2. No figure description in the report contains visual content not present in the \
corresponding Figures sub-section of an extracted file.
3. "Confirms" is not used unless connections.md documents a direct quantitative \
comparison that supports it.
4. No step is described as executed that appears in the Goal vs. Executed map in \
connections.md as "no".
5. No physics mechanism is named that does not appear in any extracted file.
6. The Key Parameters table appears exactly once in the report.
7. The Key Findings section does not restate sentences that already appear in Results.

Output format — write exactly:
## Critique

### Item 1: <one-line description>
**Result:** PASS  (or FAIL)
<if FAIL: quoted sentence and explanation>

### Item 2: ...
...

## Summary
PASS (all items passed) or FAIL (list the item numbers that failed).
"""


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _find_report(out_dir: Path) -> Path | None:
    candidates = sorted(
        (f for f in out_dir.glob("*.md") if f.name.startswith("[UNSIGNED]")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def main(out_dir: Path) -> int:
    print(f"[phase-d] Starting critique for: {out_dir}", flush=True)

    report_path = _find_report(out_dir)
    if not report_path:
        print("[phase-d] No [UNSIGNED] report found — aborting.", flush=True)
        return 1

    parts: list[str] = [f"## Report: {report_path.name}\n\n{_read(report_path)}"]
    print(f"[phase-d] Loaded report: {report_path.name}", flush=True)

    for name in ("extracted_github.md", "extracted_labarchives.md",
                 "extracted_deps.md", "extracted_dr.md", "connections.md"):
        text = _read(out_dir / name)
        if text:
            parts.append(f"## {name}\n\n{text}")
            print(f"[phase-d] Loaded {name}", flush=True)

    user_content = "\n\n---\n\n".join(parts)

    print("[phase-d] Calling API...", flush=True)
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )
    result = resp.content[0].text

    (out_dir / "critique.md").write_text(result, encoding="utf-8")
    print("[phase-d] Wrote critique.md", flush=True)

    passed = "## Summary\nPASS" in result or "**Summary:** PASS" in result.upper() or result.upper().count("FAIL") == 0
    if passed:
        print("[phase-d] All items PASSED.", flush=True)
        return 0
    else:
        print("[phase-d] Some items FAILED — orchestrator should trigger revision.", flush=True)
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    out = Path(sys.argv[1])
    if not out.is_absolute():
        out = _PROJECT_ROOT / out
    if not out.is_dir():
        print(f"Error: {out} is not a directory", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(out))
