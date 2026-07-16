from __future__ import annotations

import csv
import io
import json
import uuid

from ..models import Artifact, ExperimentConfig


TEXT_SUFFIXES = {".md", ".txt", ".log", ".json"}


# ---------------------------------------------------------------------------
# Content-level parsers (accept str, not Path — shared with GitHubAdapter)
# ---------------------------------------------------------------------------

def _parse_notebook_from_text(text: str) -> object:
    """Parse raw notebook JSON text into a NotebookNode, filling missing cell ids."""
    import nbformat
    data = json.loads(text)
    for cell in data.get("cells", []):
        if "id" not in cell:
            cell["id"] = uuid.uuid4().hex[:8]
    return nbformat.reads(json.dumps(data), as_version=4)


def notebook_text_from_content(text: str) -> str:
    """Return a flat text representation of a notebook (all cells, labelled by type)."""
    nb = _parse_notebook_from_text(text)
    parts: list[str] = []
    for i, cell in enumerate(nb.cells, start=1):
        header = f"\n## Cell {i} [{cell.cell_type}]\n"
        source = cell.get("source", "")
        parts.append(header + source)
    return "\n".join(parts).strip()


def notebook_markdown_from_content(text: str) -> list[str]:
    """Return the source text of every non-empty markdown cell in a notebook."""
    nb = _parse_notebook_from_text(text)
    return [
        cell["source"].strip()
        for cell in nb.cells
        if cell.cell_type == "markdown" and cell.get("source", "").strip()
    ]


def csv_rows_from_text(text: str) -> list[list[str]]:
    """Parse CSV text into a list of rows (including header); skip blank rows."""
    return [row for row in csv.reader(io.StringIO(text)) if row]


def csv_table_from_rows(rows: list[list[str]]) -> str:
    """Render a list of CSV rows as a markdown table string."""
    if not rows:
        return ""
    n_cols = len(rows[0])

    def _cell(v: str) -> str:
        return v.replace("|", "\\|")

    header = "| " + " | ".join(_cell(c) for c in rows[0]) + " |"
    sep    = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body_lines = []
    for row in rows[1:]:
        # Pad short rows to header width so the table stays rectangular.
        padded = (row + [""] * n_cols)[:n_cols]
        body_lines.append("| " + " | ".join(_cell(c) for c in padded) + " |")
    body = "\n".join(body_lines)
    return "\n".join(filter(None, [header, sep, body]))


def iter_all_artifacts(config: ExperimentConfig) -> list[Artifact]:
    groups = config.artifacts
    return (
        groups.primary_execution
        + groups.supporting_context
        + groups.outputs
        + groups.instrument_logs
        + groups.imaging
        + groups.design_files
    )
