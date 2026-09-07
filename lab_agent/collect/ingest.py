from __future__ import annotations

import base64
import csv
import io
import json
import re
import uuid

from ..models import Artifact, ExperimentConfig


TEXT_SUFFIXES = {".md", ".txt", ".log", ".json"}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Per-output text cap so a logging-heavy sweep doesn't blow up notebooks.md.
_OUTPUT_CHAR_CAP = 4000


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


def _as_text(val) -> str:
    """Notebook source/text fields may be a string or a list of line-strings."""
    if isinstance(val, list):
        return "".join(val)
    return val or ""


def _truncate(s: str, cap: int = _OUTPUT_CHAR_CAP) -> str:
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n[truncated {len(s) - cap} chars]"


def serialize_notebook(text: str, image_prefix: str = "") -> tuple[str, list[tuple[str, bytes]]]:
    """Serialize a notebook to markdown text plus its extracted output images.

    Emits every cell's source under a typed header — code cells include the
    ``execution_count`` (or ``unexecuted``) so an out-of-order/unrun notebook is
    visible — and, for code cells, an ``### Output`` block rendering:
      * ``stream``                            → text verbatim (capped)
      * ``execute_result`` / ``display_data`` → ``text/plain`` (capped); any
        ``image/png`` is decoded and returned as ``(filename, bytes)`` with a
        ``[output image: github_images/<filename>]`` marker so the analyst can
        open the actual result plot
      * ``error``                             → ``ename: evalue`` + last ~10
        traceback lines, ANSI-stripped

    Returns ``(markdown_text, [(filename, png_bytes), ...])``. Filenames are
    prefixed with *image_prefix* to stay unique across notebooks.
    """
    nb = _parse_notebook_from_text(text)
    parts: list[str] = []
    images: list[tuple[str, bytes]] = []
    for i, cell in enumerate(nb.cells, start=1):
        ctype = cell.cell_type
        if ctype == "code":
            ec = cell.get("execution_count")
            ec_str = f"execution_count={ec}" if ec is not None else "unexecuted"
            parts.append(f"\n## Cell {i} [code] ({ec_str})\n")
        else:
            parts.append(f"\n## Cell {i} [{ctype}]\n")
        parts.append(_as_text(cell.get("source", "")))

        if ctype != "code":
            continue
        out_lines: list[str] = []
        for k, out in enumerate(cell.get("outputs", []) or []):
            otype = out.get("output_type")
            if otype == "stream":
                out_lines.append(_truncate(_as_text(out.get("text"))))
            elif otype in ("execute_result", "display_data"):
                data = out.get("data", {}) or {}
                if "text/plain" in data:
                    out_lines.append(_truncate(_as_text(data.get("text/plain"))))
                png = data.get("image/png")
                if png:
                    try:
                        img_bytes = base64.b64decode(png)
                    except Exception:
                        img_bytes = None
                    if img_bytes:
                        name = f"{image_prefix}cell{i}_out{k}.png"
                        images.append((name, img_bytes))
                        out_lines.append(f"[output image: github_images/{name}]")
            elif otype == "error":
                tb = _ANSI_RE.sub("", "\n".join(out.get("traceback", []) or []))
                tb_tail = "\n".join(tb.splitlines()[-10:])
                out_lines.append(_truncate(
                    f"{out.get('ename', '')}: {out.get('evalue', '')}\n{tb_tail}"))
        if out_lines:
            parts.append("\n### Output\n" + "\n".join(out_lines))
    return "\n".join(parts).strip(), images


def notebook_text_from_content(text: str) -> str:
    """Flat text of a notebook (code + outputs); image bytes discarded."""
    return serialize_notebook(text)[0]


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
