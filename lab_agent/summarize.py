from __future__ import annotations

import csv
import io

from .models import CollectedArtifact, ExperimentBundle, StructuredSummary

# CSVs with more data rows than this are summarised rather than rendered as a table.
_LARGE_CSV_THRESHOLD = 20


def _summarize_large_csv(path: str, rows: list[list[str]]) -> str:
    """Return a concise one-paragraph summary for a CSV too large to render as a table."""
    if len(rows) < 2:
        return f"**{path}** — empty CSV."
    header = rows[0]
    data = rows[1:]
    col_names = ", ".join(header)

    ranges: list[str] = []
    for col_i, col_name in enumerate(header):
        vals: list[float] = []
        for row in data:
            if len(row) > col_i:
                try:
                    vals.append(float(row[col_i]))
                except ValueError:
                    pass
        if vals:
            ranges.append(f"{col_name}: [{min(vals):.4g}, {max(vals):.4g}]")

    range_str = "; ".join(ranges) if ranges else "no numeric columns"
    return (
        f"**{path}** — {len(data)} rows × {len(header)} columns ({col_names}).\n"
        f"Numeric ranges: {range_str}."
    )


def summarize_bundle(bundle: ExperimentBundle) -> StructuredSummary:
    cfg = bundle.config

    missing_information: list[str] = []
    evidence_map: list[str] = []
    labarchives_context: list[str] = []

    for art in bundle.collected_artifacts:
        if not art.exists:
            missing_information.append(f"Missing artifact: {art.path}")
            continue
        if art.source == "labarchives" and art.content:
            labarchives_context.append(art.content)
        evidence_map.append(f"{art.path} ({art.kind})")

    if not cfg.artifacts.primary_execution:
        missing_information.append("No primary notebook or execution artifact listed.")

    csv_tables: list[str] = []
    csv_summaries: list[str] = []
    for art in bundle.collected_artifacts:
        if not art.exists or not art.csv_rows or not art.content:
            continue
        data_rows = len(art.csv_rows) - 1
        if data_rows > _LARGE_CSV_THRESHOLD:
            csv_summaries.append(_summarize_large_csv(art.path, art.csv_rows))
        else:
            csv_tables.append(art.content)

    notebook_markdown = [
        cell
        for art in bundle.collected_artifacts
        if art.kind == "notebook"
        for cell in art.markdown_cells
    ]

    return StructuredSummary(
        experiment_id=cfg.experiment.id,
        title=cfg.experiment.title,
        research_question=cfg.objective.research_question,
        missing_information=missing_information,
        evidence_map=evidence_map,
        csv_tables=csv_tables,
        csv_summaries=csv_summaries,
        notebook_markdown=notebook_markdown,
        labarchives_context=labarchives_context,
    )
