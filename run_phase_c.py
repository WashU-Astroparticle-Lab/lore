"""
run_phase_c.py — Phase C report writer via direct Anthropic API (streaming).

Replaces the Agent tool spawn in Phase C with a direct streaming API call,
eliminating the ~30-minute Claude Code subagent startup overhead.

Reads:
  extracted_github.md, extracted_labarchives.md, extracted_deps.md
  extracted_dr.md (if present), connections.md, metadata.json

Writes:
  [UNSIGNED] <experiment_id>.md

With --revision: reads the existing [UNSIGNED] report and critique.md,
applies targeted fixes only to failing items, and overwrites the report.

Usage:
    python run_phase_c.py outputs/<experiment_id>
    python run_phase_c.py outputs/<experiment_id> --revision
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent.resolve()
load_dotenv(_PROJECT_ROOT / ".env", override=True)

import anthropic

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 16000

_SYSTEM = """\
You are the Report Writer for a physics lab AI pipeline.

## Your job
Write a complete, structured experiment report in Markdown. You receive structured
extractions from multiple analysts (Phase A) and a cross-source synthesis (Phase B).
You must not read raw data files — use only what is provided in the extractions.

## Report format

Begin with exactly this header block — one title line only, no filename, no .md extension:
```
# [UNSIGNED] <experiment_id>

**Experiment:** <experiment_id>
**LabArchives pages:** <page_1> · <page_2> · ...
**Report generated:** <YYYY-MM-DD>
```
Do not repeat the title. Do not include the filename or a .md extension anywhere.

Then write the following sections in order, with a horizontal rule (---) between each:

**1. Table of Contents** — linked list of all section headings

**2. Executive Summary** — one paragraph: what was done, key result, one-line physical
interpretation

**3. Objectives** — short bulleted list. Written for expert lab members — no explanation
of basic concepts. Focus only on what makes this experiment distinct.

**4. Key Parameters** — one formatted table of all critical instrument settings and
software constants. Include DR temperature row if extracted_dr.md is present.
This table appears EXACTLY ONCE — do not create a second numeric table in Results.

**5. Methods and Workflow** — instruments, software, and processing steps in prose.
Bold sub-headers for each phase (e.g. **Instruments.**, **Sweep procedure.**).
Incorporate dependency knowledge naturally from extracted_deps.md.

**6. Results** — interpret the numbers; reference specific values; note trends and
anomalies; embed relevant figures. For each figure: use ONLY the description from
the Figures sub-section of an extracted file. If none exists, write
[Image not available — not described in extraction]. One sentence of physical
interpretation per figure. Order figures by importance: primary result plots first.

**7. Key Findings and Interpretation** — what the results mean physically. Use
connections.md to ground cross-source interpretations. Do NOT restate the Results
narrative — interpret causality and underlying physics only.

**8. Dilution Refrigerator Conditions** — include ONLY if extracted_dr.md is present.
Summarise system state, temperature stability, physical implications.

**9. Open Questions** — bullet list of unresolved issues, anomalies, and follow-up
experiments. Include anything flagged in connections.md that could not be resolved.

**10. Sources** — bullet list: GitHub repo/notebooks (linked), LabArchives pages
(linked if URL known), local output files (filename only), images grouped by source.

## Figure embedding rules
- Use relative Markdown paths: ![caption](labarchives_images/filename.png)
- Place each figure directly after the paragraph that discusses it
- Only embed figures you EXPLICITLY cite in Results or Key Findings
- Do not embed every figure — only the most critical ones; budget ~5-12 figures
- Most critical figures first (primary result plots before supporting/diagnostic)
- Use descriptive captions that identify what the figure shows

## Accuracy rules
- Never invent results or describe measurements that did not occur
- Do not name physics mechanisms not mentioned in an extracted file
- The Goal vs. Executed map in connections.md is authoritative:
  if a goal is "no", do not describe it as executed — note the gap explicitly
- Use "is consistent with" or "suggests" instead of "confirms" unless connections.md
  documents a direct quantitative comparison
- Do not say "the device is working as expected" without stating the specific evidence
- Distinguish observed facts from inferences ("the data show..." vs "this suggests...")
- Never read [UNSIGNED] files — they are prior AI drafts and may contain errors

## Style
- Prose paragraphs unless the section says bulleted list
- Keep tight: one well-chosen sentence beats two vague ones
- Scale depth to the data — a quick single sweep does not need a long report
"""

_REVISION_SYSTEM = """\
You are the Report Writer for a physics lab AI pipeline, performing a targeted revision.

A Phase D critique has flagged specific FAIL items in the existing report. Your job is
to fix ONLY those failing items — make the minimum changes necessary. Do not restructure
sections that PASSED. Do not rewrite passing sentences.

Output ONLY the complete revised report — all sections, starting directly with the
# [UNSIGNED] header line. Do NOT write any preamble, explanation of changes, "Fixes
Applied" section, or commentary before or after the report. The output must be a
clean Markdown document that can be uploaded directly to LabArchives.
"""


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _clean_report(text: str) -> str:
    """Strip any preamble before the first # heading and remove .md from the title line."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title = line.rstrip()
            if title.endswith(".md"):
                title = title[:-3]
            return title + "\n" + "\n".join(lines[i + 1:])
    return text


def _find_report(out_dir: Path) -> Path | None:
    candidates = sorted(
        (f for f in out_dir.glob("*.md") if f.name.startswith("[UNSIGNED]")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _load_metadata(out_dir: Path) -> dict:
    meta_path = out_dir / "metadata.json"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _stream(system: str, user_content: str) -> str:
    client = anthropic.Anthropic()
    parts: list[str] = []
    with client.messages.stream(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    ) as stream:
        for text in stream.text_stream:
            parts.append(text)
    return "".join(parts)


def run_report(out_dir: Path) -> None:
    meta = _load_metadata(out_dir)
    experiment_id = meta.get("experiment_id") or out_dir.name
    la_pages = meta.get("la_pages") or []
    report_date = date.today().isoformat()

    print(f"[phase-c] Writing report for: {experiment_id}", flush=True)

    parts: list[str] = []

    # Metadata block passed to the model
    meta_block = (
        f"experiment_id: {experiment_id}\n"
        f"la_pages: {' · '.join(la_pages) if la_pages else '(unknown)'}\n"
        f"report_date: {report_date}"
    )
    parts.append(f"## Metadata\n\n{meta_block}")

    for name in ("extracted_github.md", "extracted_labarchives.md",
                 "extracted_deps.md", "extracted_dr.md", "connections.md"):
        text = _read(out_dir / name)
        if text:
            parts.append(f"## {name}\n\n{text}")
            print(f"[phase-c] Loaded {name}", flush=True)

    if not any("extracted_" in p for p in parts):
        print("[phase-c] No extracted files found — aborting.", flush=True)
        sys.exit(1)

    user_content = "\n\n---\n\n".join(parts)

    print("[phase-c] Calling API (streaming)...", flush=True)
    result = _clean_report(_stream(_SYSTEM, user_content))

    report_path = out_dir / f"[UNSIGNED] {experiment_id}.md"
    report_path.write_text(result, encoding="utf-8")
    print(f"[phase-c] Wrote {report_path.name}", flush=True)
    print("[phase-c] Phase C complete.", flush=True)


def run_revision(out_dir: Path) -> None:
    meta = _load_metadata(out_dir)
    experiment_id = meta.get("experiment_id") or out_dir.name

    print(f"[phase-c] Revision pass for: {experiment_id}", flush=True)

    report_path = _find_report(out_dir)
    if not report_path:
        print("[phase-c] No [UNSIGNED] report to revise — aborting.", flush=True)
        sys.exit(1)

    critique = _read(out_dir / "critique.md")
    if not critique:
        print("[phase-c] No critique.md found — aborting.", flush=True)
        sys.exit(1)

    parts: list[str] = [
        f"## Current report: {report_path.name}\n\n{_read(report_path)}",
        f"## critique.md\n\n{critique}",
    ]
    for name in ("extracted_github.md", "extracted_labarchives.md",
                 "extracted_deps.md", "connections.md"):
        text = _read(out_dir / name)
        if text:
            parts.append(f"## {name}\n\n{text}")

    user_content = "\n\n---\n\n".join(parts)

    print("[phase-c] Calling API for revision (streaming)...", flush=True)
    result = _clean_report(_stream(_REVISION_SYSTEM, user_content))

    report_path.write_text(result, encoding="utf-8")
    print(f"[phase-c] Revised {report_path.name}", flush=True)
    print("[phase-c] Revision complete.", flush=True)


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

    revision = "--revision" in sys.argv
    if revision:
        run_revision(out)
    else:
        run_report(out)
