"""
run_phase_a.py — Phase A extraction agents via direct Anthropic API.

Replaces the 4 Agent tool spawns in Phase A with direct Sonnet API calls
using ANTHROPIC_API_KEY from .env instead of the Claude Code Pro plan.

Runs all analysts in parallel and writes:
  extracted_github.md
  extracted_labarchives.md
  extracted_deps.md
  extracted_dr.md  (only if dr_conditions.md exists)

Usage:
    python run_phase_a.py outputs/<experiment_id>
"""
from __future__ import annotations

import base64
import concurrent.futures
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent.resolve()
load_dotenv(_PROJECT_ROOT / ".env", override=True)

import anthropic

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 32000
_MAX_IMAGES = 20          # max images to read from manifests
_MAX_IMAGE_BYTES = 4 * 1024 * 1024   # 4 MB total across all images per analyst call

_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _read_lab_config(key: str) -> str:
    """Read a value from lab_config.md markdown table by key name."""
    text = _read(_PROJECT_ROOT / "lab_config.md")
    m = re.search(rf"\|\s*{re.escape(key)}\s*\|\s*([^|\n]+)\|", text)
    return m.group(1).strip() if m else ""


def _parse_image_paths(manifest_text: str) -> list[Path]:
    """Extract image paths from a *_images.md manifest (absolute paths).

    No hard count cap — the per-analyst byte cap (_MAX_IMAGE_BYTES) governs
    how many actually get sent to the API after resizing.
    """
    paths: list[Path] = []
    for m in re.finditer(r"^- `([^`]+)`", manifest_text, re.MULTILINE):
        p = Path(m.group(1))
        if p.exists() and p.suffix.lower() in _EXT_TO_MIME:
            paths.append(p)
    return paths


def _image_block(img_path: Path) -> tuple[dict, int]:
    """Return (content_block, byte_size_after_resize).

    Resizes to at most 900 px wide (JPEG q=80) using PIL if available,
    so each image stays small for the API payload.
    Falls back to raw bytes when PIL is unavailable.
    """
    try:
        from PIL import Image
        import io
        with Image.open(img_path) as im:
            im = im.convert("RGB")
            if im.width > 900:
                ratio = 900 / im.width
                im = im.resize((900, int(im.height * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80, optimize=True)
            img_bytes = buf.getvalue()
        mime = "image/jpeg"
    except Exception:
        mime = _EXT_TO_MIME.get(img_path.suffix.lower(), "image/png")
        img_bytes = img_path.read_bytes()

    data = base64.standard_b64encode(img_bytes).decode()
    block = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
    return block, len(img_bytes)


def _call(system: str, user_blocks: list[dict]) -> str:
    client = anthropic.Anthropic()
    text_parts: list[str] = []
    with client.messages.stream(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_blocks}],
    ) as stream:
        for text in stream.text_stream:
            text_parts.append(text)
    return "".join(text_parts)


def _fetch_wiring_diagram() -> str:
    """Fetch the wiring diagram page from LabArchives."""
    page = _read_lab_config("Wiring diagram page")
    if not page:
        return "[Wiring diagram page not configured in lab_config.md]"
    try:
        sys.path.insert(0, str(_PROJECT_ROOT))
        from lab_agent.sources.labarchives import LabArchivesAdapter
        artifacts = LabArchivesAdapter(page).fetch()
        parts = [a.content for a in artifacts if a.content]
        return "\n\n".join(parts) if parts else "[Wiring diagram page returned no content]"
    except Exception as exc:
        return f"[Could not fetch wiring diagram: {exc}]"


# ---------------------------------------------------------------------------
# Analysts
# ---------------------------------------------------------------------------

_GITHUB_SYSTEM = """\
You are the GitHub Analyst. Extract structured information from the experiment's \
notebook files and produce a single Markdown document with exactly these sections \
(use ## headings):

## Key Parameters
A markdown table: Parameter | Value | Units | Notes
Every instrument setting, frequency range, amplitude, power level, timing parameter, \
and software constant found anywhere. If a value appears multiple times with different \
numbers, list both and note the discrepancy.

## Sweep Procedure
Ordered list of what the code actually does step by step. Interpret imported \
classes/functions from their names and usage — do not guess at internals not visible \
in the notebooks.

## Numeric Results
Bulleted list of every quantitative outcome with units: fitted resonance frequencies, \
Q factors, power levels, measured ranges, calibration values.

## Figures
One sub-section (### filename) per image provided. Describe what is visually present: \
axis labels, curve shapes, legend entries, numeric values readable in the plot. \
One-line physical interpretation. If an image cannot be read write [Image not readable].

## Cross-reference flags
List values that need validation against other sources: attenuation assumptions baked \
into amplitude settings, frequency references that should match LabArchives notes, \
timestamps visible in notebooks that could correlate with DR data.
"""

_LABARCHIVES_SYSTEM = """\
You are the LabArchives Analyst. Extract structured information from the lab notebook \
pages and wiring diagram and produce a single Markdown document with exactly these \
sections (use ## headings):

## Timeline
Chronological list of actions and observations with timestamps as they appear in the \
lab notebook.

## Lab Observations
What was actually observed or noted during the experiment — written in past tense, \
sourced from what the notebook says happened.

## Stated Goals
Clearly labelled NOT necessarily executed. Every future-tense statement, "plan to", \
"optionally", or "next we will" found in the notes. These are intentions, not actions.

## All Hyperlinks
Every [text](url) link found anywhere — preserve the full URL. Note briefly what \
each link points to.

## Additional GitHub URLs
Any GitHub links that appear in the content beyond the primary one. For each, note \
whether it appears relevant to this experiment based on context.

## Attenuation Chain
From the wiring diagram section: full RF component list in signal-path order with \
the dB value for each stage and the total attenuation.

## Discrepancies
Any attenuation or power value in the lab notes that conflicts with the wiring \
diagram — quote both values exactly and note which source is the diagram.

## Figures
One sub-section (### filename) per image provided. Describe visual content. \
Physical interpretation. Write [Image not readable] if the file cannot be opened.

## Cross-reference flags
Values in the lab notes that need validation against GitHub notebooks — power levels \
assumed in notes, frequency references, timing windows that should align with \
experiment code.
"""

_DEPS_SYSTEM = """\
You are the Dependencies Analyst. Extract structured information from lab-specific \
package source code and produce a single Markdown document.

One ## section per package: what the package does, key classes with their constructor \
parameters and defaults, key hardware constants defined in the source, data flow \
through the package's main entry points.

Note any packages marked "Not found in org" and describe what their import usage in \
the notebooks suggests about their role.

## Cross-reference flags
Any constant whose default value in the source differs from what appears to be set \
explicitly in the notebooks.
"""

_DR_SYSTEM = """\
You are the DR Analyst. Interpret dilution refrigerator log data and produce a single \
Markdown document with exactly these sections (use ## headings):

## System State
Was the system at base temperature, cooling, or warming during the measurement window? \
One clear sentence.

## Temperature Analysis
MXC min/median/max with units. Ratio of max to min. Which channels are reporting valid \
readings vs. known-unreliable at base temperature. Note: RuO2 sensors (Still, 50 mK \
plate) lose calibration below ~1 K and return garbage at base — their absence is normal.

## Pressure Analysis
P1 fore-line value, whether pumps were running, any fluctuations and what they indicate.

## n_th calculation
Thermal photon occupancy at 5 GHz at the MXC median temperature using \
n_th = 1/(exp(hf/kT) - 1). Show the arithmetic.

## Quasiparticle assessment
Using Mattis-Bardeen physics: estimate the thermal quasiparticle contribution relative \
to base and what it means for the experiment type. For Al: Delta ~ 172 ueV ~ 2 K \
equivalent. n_qp ∝ exp(-Delta/kT).

## Anomaly flags
Each anomaly that applies, with its physical explanation:
- MXC min > 50 mK: system did not reach proper base, thermal photons and QP density elevated
- Large MXC spread (max/min > 3x): still cooling, thermal event, or warming
- MXC median >> MXC min: system spent most of window warmer than coldest point
- Still > 1 K: reduced 3He circulation
- P1 at atmospheric (~1000 mbar): fore-pump was off, no active cooling

## Cross-reference flags
Time windows where the DR was anomalous that should be checked against experiment \
timestamps visible in the GitHub notebooks.
"""


# ---------------------------------------------------------------------------
# Per-analyst runners
# ---------------------------------------------------------------------------

def run_github_analyst(out_dir: Path) -> str:
    print("[phase-a] GitHub Analyst: reading files...", flush=True)
    notebooks = _read(out_dir / "notebooks.md")
    data = _read(out_dir / "data_summaries.md")
    img_manifest = _read(out_dir / "github_images.md")
    image_paths = _parse_image_paths(img_manifest)

    user_blocks: list[dict] = []
    total_bytes = 0
    included = 0
    if notebooks:
        user_blocks.append({"type": "text", "text": f"## notebooks.md\n\n{notebooks}"})
    if data:
        user_blocks.append({"type": "text", "text": f"## data_summaries.md\n\n{data}"})
    if image_paths:
        user_blocks.append({"type": "text", "text": f"## GitHub Images ({len(image_paths)} provided)"})
        for img_path in image_paths:
            user_blocks.append({"type": "text", "text": f"### {img_path.name}"})
            try:
                block, size = _image_block(img_path)
                if total_bytes + size > _MAX_IMAGE_BYTES:
                    user_blocks.append({"type": "text", "text": f"[{img_path.name}: skipped — payload cap reached]"})
                    continue
                user_blocks.append(block)
                total_bytes += size
                included += 1
            except Exception as exc:
                user_blocks.append({"type": "text", "text": f"[Could not load image: {exc}]"})

    if not user_blocks:
        return "[GitHub Analyst: no input files found]"

    print(f"[phase-a] GitHub Analyst: calling API ({included}/{len(image_paths)} images, "
          f"{total_bytes // 1024} KB)...", flush=True)
    result = _call(_GITHUB_SYSTEM, user_blocks)
    print("[phase-a] GitHub Analyst: done.", flush=True)
    return result


def run_labarchives_analyst(out_dir: Path) -> str:
    print("[phase-a] LabArchives Analyst: reading files...", flush=True)
    la_notes = _read(out_dir / "labarchives.md")
    img_manifest = _read(out_dir / "labarchives_images.md")
    image_paths = _parse_image_paths(img_manifest)

    print("[phase-a] LabArchives Analyst: fetching wiring diagram...", flush=True)
    wiring = _fetch_wiring_diagram()

    user_blocks: list[dict] = []
    total_bytes = 0
    included = 0
    if la_notes:
        user_blocks.append({"type": "text", "text": f"## labarchives.md\n\n{la_notes}"})
    if wiring:
        user_blocks.append({"type": "text", "text": f"## Wiring Diagram (live from LabArchives)\n\n{wiring}"})
    if image_paths:
        user_blocks.append({"type": "text", "text": f"## LabArchives Images ({len(image_paths)} provided)"})
        for img_path in image_paths:
            user_blocks.append({"type": "text", "text": f"### {img_path.name}"})
            try:
                block, size = _image_block(img_path)
                if total_bytes + size > _MAX_IMAGE_BYTES:
                    user_blocks.append({"type": "text", "text": f"[{img_path.name}: skipped — payload cap reached]"})
                    continue
                user_blocks.append(block)
                total_bytes += size
                included += 1
            except Exception as exc:
                user_blocks.append({"type": "text", "text": f"[Could not load image: {exc}]"})

    if not user_blocks:
        return "[LabArchives Analyst: no input files found]"

    print(f"[phase-a] LabArchives Analyst: calling API ({included}/{len(image_paths)} images, "
          f"{total_bytes // 1024} KB)...", flush=True)
    result = _call(_LABARCHIVES_SYSTEM, user_blocks)
    print("[phase-a] LabArchives Analyst: done.", flush=True)
    return result


def run_deps_analyst(out_dir: Path) -> str:
    print("[phase-a] Dependencies Analyst: reading files...", flush=True)
    deps = _read(out_dir / "dependencies.md")
    if not deps:
        return "[Dependencies Analyst: dependencies.md not found]"

    user_blocks = [{"type": "text", "text": f"## dependencies.md\n\n{deps}"}]
    print("[phase-a] Dependencies Analyst: calling API...", flush=True)
    result = _call(_DEPS_SYSTEM, user_blocks)
    print("[phase-a] Dependencies Analyst: done.", flush=True)
    return result


def run_dr_analyst(out_dir: Path) -> str | None:
    dr_file = out_dir / "dr_conditions.md"
    if not dr_file.exists():
        return None

    print("[phase-a] DR Analyst: reading files...", flush=True)
    dr_data = _read(dr_file)
    user_blocks = [{"type": "text", "text": f"## dr_conditions.md\n\n{dr_data}"}]
    print("[phase-a] DR Analyst: calling API...", flush=True)
    result = _call(_DR_SYSTEM, user_blocks)
    print("[phase-a] DR Analyst: done.", flush=True)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(out_dir: Path) -> None:
    print(f"[phase-a] Starting Phase A extractions for: {out_dir}", flush=True)

    futures: dict[str, concurrent.futures.Future] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures["github"] = pool.submit(run_github_analyst, out_dir)
        futures["labarchives"] = pool.submit(run_labarchives_analyst, out_dir)
        futures["deps"] = pool.submit(run_deps_analyst, out_dir)
        futures["dr"] = pool.submit(run_dr_analyst, out_dir)

    results = {k: f.result() for k, f in futures.items()}

    # Write outputs
    if results["github"]:
        (out_dir / "extracted_github.md").write_text(results["github"], encoding="utf-8")
        print("[phase-a] Wrote extracted_github.md", flush=True)

    if results["labarchives"]:
        (out_dir / "extracted_labarchives.md").write_text(results["labarchives"], encoding="utf-8")
        print("[phase-a] Wrote extracted_labarchives.md", flush=True)

    if results["deps"]:
        (out_dir / "extracted_deps.md").write_text(results["deps"], encoding="utf-8")
        print("[phase-a] Wrote extracted_deps.md", flush=True)

    if results["dr"] is not None:
        (out_dir / "extracted_dr.md").write_text(results["dr"], encoding="utf-8")
        print("[phase-a] Wrote extracted_dr.md", flush=True)
    else:
        print("[phase-a] No dr_conditions.md found — skipping DR Analyst.", flush=True)

    print("[phase-a] Phase A complete.", flush=True)


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
