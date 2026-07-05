"""
run_phase_b.py — Phase B synthesis via direct Anthropic API.

Reads all extracted_*.md files and writes connections.md.
Uses ANTHROPIC_API_KEY from .env (not the Claude Code Pro plan).

Usage:
    python run_phase_b.py outputs/<experiment_id>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent.resolve()
load_dotenv(_PROJECT_ROOT / ".env", override=True)

import anthropic

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 8096

_SYSTEM = """\
You are the Synthesis Agent. Your job is to read the structured extractions from \
multiple sources and find every cross-source connection, conflict, and gap. \
You read only the extracted files — not the raw data.

Write a single Markdown document called connections.md with exactly these sections \
(use ## headings):

## Power chain closure
Take the attenuation chain from extracted_labarchives.md and the amplitude/power \
settings from extracted_github.md. Calculate the power at the device (dBm). \
Does the math close? State the result with units and note any discrepancy.

## Timeline correlations
Map experiment steps from extracted_github.md against DR anomaly windows from \
extracted_dr.md — was any measurement taken during an elevated-temperature or \
pump-off period? If no DR data is present, state that explicitly.

## Lab observations vs. numeric results
Does what was noted in extracted_labarchives.md corroborate or contradict the \
fitted/measured values in extracted_github.md? Note agreements and conflicts \
separately.

## Goal vs. executed map
A table with one row per stated goal from extracted_labarchives.md:
Stated Goal | Evidence of Execution in Notebooks (yes/no/partial) | Source line

## Dependency constants vs. notebook usage
Using extracted_deps.md cross-reference flags, check whether notebooks override \
package defaults. List every case where the notebook sets a value that differs \
from the package default.

## Multi-source conflicts
Any numeric value that appears in more than one extracted file with different \
numbers. Exact values from each source. Which to trust and why.

## Figures needing cross-source context
Any figure described in an extracted file whose physical interpretation depends \
on information only available in a different extracted file. Explain the dependency.

## Additional GitHub URLs recommendation
Based on context from all extracted files, recommend whether any additional GitHub \
URLs flagged by the LabArchives Analyst are worth fetching before the report is \
written. If yes, note which ones and why.
"""


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def main(out_dir: Path) -> None:
    print(f"[phase-b] Starting synthesis for: {out_dir}", flush=True)

    parts: list[str] = []
    for name in ("extracted_github.md", "extracted_labarchives.md",
                 "extracted_deps.md", "extracted_dr.md"):
        text = _read(out_dir / name)
        if text:
            parts.append(f"## {name}\n\n{text}")
            print(f"[phase-b] Loaded {name}", flush=True)

    if not parts:
        print("[phase-b] No extracted files found — aborting.", flush=True)
        sys.exit(1)

    user_content = "\n\n---\n\n".join(parts)

    print("[phase-b] Calling API...", flush=True)
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )
    result = resp.content[0].text

    (out_dir / "connections.md").write_text(result, encoding="utf-8")
    print("[phase-b] Wrote connections.md", flush=True)
    print("[phase-b] Phase B complete.", flush=True)


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
    main(out)
