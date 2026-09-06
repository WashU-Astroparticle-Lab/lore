"""OKF (Open Knowledge Format) helpers — self-describing markdown concepts.

OKF Phase 1: every output file gets a small YAML frontmatter block so the run
directory is a browsable, machine-readable knowledge bundle, and a reserved
``index.md`` lists the concepts for progressive disclosure.

Frontmatter is kept **deterministic** (source-derived timestamps only, never the
run time) so the eval golden-diff stays stable; the run timestamp lives solely in
metadata.json. Consumers must stay permissive — a missing frontmatter block or an
unknown type is never an error.
"""
from __future__ import annotations

import re
from pathlib import Path

# Collection-stage output files → their OKF concept type.
_FILE_TYPES = {
    "notebooks.md": "Notebooks",
    "scripts.md": "Scripts",
    "labarchives.md": "Lab Notes",
    "data_summaries.md": "Data Summary",
    "dependencies.md": "Dependencies",
    "github_images.md": "Image Manifest",
    "labarchives_images.md": "Image Manifest",
    "wiring_diagram.md": "Reference",
    "dr_conditions.md": "DR Conditions",
}

# Files carrying GitHub-derived provenance (the rest are LabArchives/other).
_GITHUB_FILES = {"notebooks.md", "scripts.md", "data_summaries.md",
                 "dependencies.md", "github_images.md"}


def frontmatter(concept_type: str, *, resource: str = "", source_ref: str | None = None,
                timestamp: str | None = None, tags: list[str] | None = None) -> str:
    """Build a YAML frontmatter block (``type`` is the only required key)."""
    lines = ["---", f"type: {concept_type}"]
    if resource:
        lines.append(f"resource: {resource}")
    if source_ref:
        lines.append(f"source_ref: {source_ref}")
    if timestamp:
        lines.append(f"timestamp: {timestamp}")
    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def generate_index(out_dir: Path) -> str:
    """OKF index.md: list every concept file (except index.md) with its type."""
    rows: list[str] = []
    for f in sorted(out_dir.glob("*.md")):
        if f.name == "index.md":
            continue
        head = f.read_text(encoding="utf-8", errors="replace")[:400]
        m = re.search(r"^type:\s*(.+)$", head, re.M)
        ctype = m.group(1).strip() if m else "Unknown"
        rows.append(f"- [{f.name}]({f.name}) — {ctype}")
    body = "# Index\n\n" + ("\n".join(rows) if rows else "(no concept files)") + "\n"
    return frontmatter("Index", resource=out_dir.name) + body


def finalize_bundle(out_dir: str | Path, *, resource: str = "", source_ref: str | None = None,
                    timestamp: str | None = None, tags: list[str] | None = None) -> None:
    """Prepend frontmatter to each collection output file (idempotent) + write index.md."""
    out_dir = Path(out_dir)
    for name, ctype in _FILE_TYPES.items():
        f = out_dir / name
        if not f.exists():
            continue
        body = f.read_text(encoding="utf-8", errors="replace")
        if body.lstrip().startswith("---"):
            continue  # already has frontmatter
        is_gh = name in _GITHUB_FILES
        f.write_text(
            frontmatter(
                ctype,
                resource=resource if is_gh else "",
                source_ref=source_ref if is_gh else None,
                timestamp=timestamp if is_gh else None,
                tags=tags,
            ) + body,
            encoding="utf-8",
        )
    (out_dir / "index.md").write_text(generate_index(out_dir), encoding="utf-8")
