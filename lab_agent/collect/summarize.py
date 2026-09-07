from __future__ import annotations

import csv
import io
import statistics
from datetime import datetime

from ..models import CollectedArtifact, ExperimentBundle, StructuredSummary

# CSVs with more data rows than this are summarised rather than rendered as a table.
_LARGE_CSV_THRESHOLD = 20

_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f",   # fractional seconds
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
    "%m/%d/%Y", "%m/%d/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S",
)


def _looks_like_timestamp(s: str) -> bool:
    s = s.strip()
    if not s:
        return False
    for fmt in _TS_FORMATS:
        try:
            datetime.strptime(s, fmt)
            return True
        except ValueError:
            continue
    return False


def _md_table(header: list[str], rows: list[list[str]]) -> str:
    n = len(header)

    def esc(v: str) -> str:
        return str(v).replace("|", "\\|")

    out = ["| " + " | ".join(esc(c) for c in header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    for r in rows:
        padded = (list(r) + [""] * n)[:n]
        out.append("| " + " | ".join(esc(c) for c in padded) + " |")
    return "\n".join(out)


def _summarize_large_csv(path: str, rows: list[list[str]]) -> str:
    """Summarise a CSV too large to render in full: per-column stats, non-numeric
    value samples, timestamp ranges, and the first/last few rows verbatim (WS6)."""
    if len(rows) < 2:
        return f"**{path}** — empty CSV."
    header = rows[0]
    data = rows[1:]
    n = len(header)

    lines: list[str] = [
        f"**{path}** — {len(data)} rows × {n} columns.",
        f"Columns: {', '.join(header)}.",
        "",
        "Per-column:",
    ]
    for ci, cname in enumerate(header):
        col_vals = [row[ci] for row in data if len(row) > ci and row[ci] != ""]
        nums: list[float] = []
        non_numeric = 0
        for v in col_vals:
            try:
                nums.append(float(v))
            except ValueError:
                non_numeric += 1
        if nums and non_numeric == 0:
            lines.append(
                f"- **{cname}** (numeric, n={len(nums)}): min={min(nums):.4g}, "
                f"max={max(nums):.4g}, mean={statistics.fmean(nums):.4g}, "
                f"median={statistics.median(nums):.4g}")
        elif nums:
            lines.append(
                f"- **{cname}** (mixed, {len(nums)} numeric / {non_numeric} non-numeric): "
                f"numeric min={min(nums):.4g}, max={max(nums):.4g}")
        elif col_vals and _looks_like_timestamp(col_vals[0]) and _looks_like_timestamp(col_vals[-1]):
            lines.append(f"- **{cname}** (timestamp): first={col_vals[0]}, last={col_vals[-1]}")
        else:
            distinct = list(dict.fromkeys(col_vals))
            shown = ", ".join(distinct[:8])
            more = f" (+{len(distinct) - 8} more distinct)" if len(distinct) > 8 else ""
            lines.append(f"- **{cname}** (text): {shown}{more}")

    lines += ["", "First rows:", _md_table(header, data[:3])]
    if len(data) > 3:
        lines += ["", "Last rows:", _md_table(header, data[-3:])]
    lines += [
        "",
        f"_Full data not included ({len(data)} rows > {_LARGE_CSV_THRESHOLD}-row "
        "threshold) — per-row claims are unverifiable from this summary._",
    ]
    return "\n".join(lines)


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
