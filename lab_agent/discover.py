"""Artifact discovery and classification for experiment ingestion."""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from .models import (
    Artifact,
    ArtifactGroup,
    ExperimentConfig,
    ExperimentMeta,
    Objective,
)

# ---------------------------------------------------------------------------
# Classification tables
# ---------------------------------------------------------------------------

_NOTEBOOK_EXT = {".ipynb"}
_CODE_EXT = {".py", ".r", ".jl", ".m", ".sh", ".sql"}
_FIGURE_EXT = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".eps", ".pdf"}
_NOTES_EXT = {".md", ".rst", ".tex", ".txt"}
_CONFIG_EXT = {".yaml", ".yml", ".toml", ".ini", ".cfg"}
_PROCESSED_DATA_EXT = {".parquet", ".npy", ".npz", ".pkl", ".hdf5", ".h5", ".feather", ".arrow", ".xlsx"}
_RAW_DATA_EXT = {".csv", ".tsv", ".log", ".dat", ".json"}

# Folder names that hint at a particular artifact category.
_FOLDER_HINTS: dict[str, str] = {
    "notebooks": "notebook",
    "nb": "notebook",
    "raw": "raw_data",
    "raw_data": "raw_data",
    "data": "raw_data",
    "processed": "processed_data",
    "outputs": "processed_data",
    "results": "processed_data",
    "figures": "figure",
    "plots": "figure",
    "figs": "figure",
    "images": "figure",
    "notes": "notes",
    "docs": "notes",
    "context": "notes",
    "src": "code",
    "scripts": "code",
    "code": "code",
}

# Notebook stem names that strongly suggest the file is the primary execution notebook.
_PRIMARY_NOTEBOOK_STEMS = {
    "main", "analysis", "notebook", "experiment",
    "run", "pipeline", "index",
}


# ---------------------------------------------------------------------------
# Per-file classification
# ---------------------------------------------------------------------------

def classify_file(path: str) -> tuple[str, str]:
    """Classify a file path into (artifact_type, description).

    Returns one of: notebook | code | raw_data | processed_data |
                    notes | figure | config | unclear

    Folder-name hints take priority; file extension is the fallback.
    """
    p = PurePosixPath(path)
    suffix = p.suffix.lower()

    # Folder hint: walk parent parts (skip last = filename)
    for part in p.parts[:-1]:
        hint = _FOLDER_HINTS.get(part.lower())
        if hint:
            if hint == "notebook" and suffix in _NOTEBOOK_EXT:
                return "notebook", f"Notebook (in {part}/)"
            elif hint == "notebook":
                return "raw_data", f"Data file (in {part}/)"
            elif hint == "raw_data":
                return "raw_data", f"Raw data (in {part}/)"
            elif hint == "processed_data":
                if suffix in _FIGURE_EXT:
                    return "figure", f"Figure (in {part}/)"
                return "processed_data", f"Processed data (in {part}/)"
            elif hint == "figure":
                return "figure", f"Figure (in {part}/)"
            elif hint == "notes":
                return "notes", f"Notes (in {part}/)"
            elif hint == "code":
                return "code", f"Code (in {part}/)"

    # Extension fallback
    if suffix in _NOTEBOOK_EXT:
        return "notebook", "Jupyter notebook"
    if suffix in _CODE_EXT:
        return "code", "Code file"
    if suffix in _NOTES_EXT:
        return "notes", "Notes / documentation"
    if suffix in _FIGURE_EXT:
        return "figure", "Figure or plot"
    if suffix in _PROCESSED_DATA_EXT:
        return "processed_data", "Processed data"
    if suffix in _CONFIG_EXT:
        return "config", "Configuration file"
    if suffix in _RAW_DATA_EXT:
        return "raw_data", "Raw data file"

    return "unclear", "Unclassified file"


def _notebook_score(path: str, folder_name: str = "") -> int:
    """Score a notebook for likelihood of being the primary execution notebook."""
    stem = PurePosixPath(path).stem.lower()
    score = 0
    folder_words = {w for w in re.split(r"[_\-\s]+", folder_name.lower()) if len(w) > 3}
    stem_words = {w for w in re.split(r"[_\-\s]+", stem) if len(w) > 3}
    if folder_words & stem_words:
        score += 2
    if stem in _PRIMARY_NOTEBOOK_STEMS or any(stem.startswith(s + "_") for s in _PRIMARY_NOTEBOOK_STEMS):
        score += 1
    return score


# ---------------------------------------------------------------------------
# ArtifactGroup construction
# ---------------------------------------------------------------------------

def build_artifact_group(file_paths: list[str], folder_name: str = "") -> ArtifactGroup:
    """Classify a list of discovered file paths into an ArtifactGroup.

    Notebooks are assigned in two passes so the most likely 'primary' notebook
    is placed in primary_execution; remaining notebooks go into supporting_context.
    """
    primary: list[Artifact] = []
    supporting: list[Artifact] = []
    outputs: list[Artifact] = []
    logs: list[Artifact] = []
    imaging: list[Artifact] = []
    design: list[Artifact] = []

    notebooks: list[tuple[str, Artifact]] = []

    for path in sorted(file_paths):
        cat, desc = classify_file(path)
        artifact = Artifact(path=path, type=cat, description=desc)

        if cat == "notebook":
            notebooks.append((path, artifact))
        elif cat == "code":
            design.append(artifact)
        elif cat in ("notes", "config", "unclear"):
            supporting.append(artifact)
        elif cat == "raw_data":
            logs.append(artifact)
        elif cat == "processed_data":
            outputs.append(artifact)
        elif cat == "figure":
            imaging.append(artifact)

    # Notebook assignment: rank by score, highest goes to primary_execution.
    if notebooks:
        ranked = sorted(notebooks, key=lambda t: _notebook_score(t[0], folder_name), reverse=True)
        primary.append(ranked[0][1])
        supporting.extend(a for _, a in ranked[1:])

    return ArtifactGroup(
        primary_execution=primary,
        supporting_context=supporting,
        outputs=outputs,
        instrument_logs=logs,
        imaging=imaging,
        design_files=design,
    )


# ---------------------------------------------------------------------------
# Notebook metadata inference
# ---------------------------------------------------------------------------

_WEAK_HEADINGS = frozenset({
    "introduction", "setup", "overview", "notebook", "imports",
    "import", "configuration", "config", "background", "data",
    "analysis", "results", "conclusion", "conclusions", "discussion",
    "methods", "method",
})


def infer_title_from_notebook(markdown_cells: list[str]) -> str | None:
    """Return the first strong H1 or H2 heading from notebook markdown cells."""
    for cell in markdown_cells:
        for line in cell.splitlines():
            m = re.match(r"^(#{1,2})\s+(.+)", line.strip())
            if not m:
                continue
            heading = m.group(2).strip()
            normalised = re.sub(r"[^\w\s]", "", heading).lower().strip()
            if normalised in _WEAK_HEADINGS:
                continue
            if len(heading) < 5:
                continue
            return heading
    return None


def infer_objective_from_notebook(markdown_cells: list[str]) -> str | None:
    """Return the first substantial paragraph after a heading in notebook markdown cells."""
    found_heading = False
    for cell in markdown_cells[:8]:
        for line in cell.splitlines():
            line = line.strip()
            if not line:
                continue
            if re.match(r"^#+\s", line):
                found_heading = True
                continue
            if re.match(r"^[\-\*\+]|\d+\.", line):
                continue
            if len(line) < 35:
                continue
            if found_heading:
                if len(line) > 250:
                    line = line[:247].rsplit(" ", 1)[0] + "…"
                return line
    return None


# ---------------------------------------------------------------------------
# Config synthesis
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "experiment"


def _titlify(name: str) -> str:
    return re.sub(r"[_\-]+", " ", name).strip().title() or "Discovered experiment"


def synthesize_config(
    folder_name: str,
    artifact_group: ArtifactGroup,
    inferred_title: str | None = None,
    inferred_objective: str | None = None,
) -> ExperimentConfig:
    """Build an ExperimentConfig from discovered artifacts.

    Precedence for each metadata field (highest to lowest):
      1. Notebook inference  (inferred_title / inferred_objective)
      2. Folder-name fallback  (slugified / titlified folder name)
    """
    exp_id = _slugify(folder_name)
    title = inferred_title or _titlify(folder_name)
    research_question = (
        inferred_objective
        or "Research question not specified — inferred from discovered artifacts."
    )

    return ExperimentConfig(
        experiment=ExperimentMeta(id=exp_id, title=title),
        objective=Objective(research_question=research_question),
        artifacts=artifact_group,
    )
