"""
Upload a finished experiment report to LabArchives (invoked via the root-level
upload_to_labarchives.py shim).

Usage:
    python upload_to_labarchives.py <out_dir> [options]

Options:
    --report-file NAME   upload this report instead of the newest "[UNSIGNED] *.md".
                         Absolute, or relative to <out_dir>. Use it whenever the
                         directory holds more than one report.
    --page-title TITLE   page title in LabArchives (default: "[UNSIGNED] <out_dir name>")
    --new-page           always create a new page, even if one with this title
                         exists. Default is to add a revision to the existing page,
                         so a re-upload does not leave duplicate pages behind.

Example:
    python upload_to_labarchives.py outputs/power_calibration_20260227
    python upload_to_labarchives.py outputs/presto_vna_spectrum_20260831 \
        --report-file "[UNSIGNED] presto_vna_spectrum_20260831_simple.md"

The report must already exist in <out_dir> (newest "[UNSIGNED] *.md", or
experiment_report.md as a fallback).
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..config import PROJECT_ROOT, load_env
from ..publish import upload_report


def _parse_args(argv: list[str]) -> tuple[str | None, dict]:
    opts: dict = {"report_file": None, "page_title": None, "new_page": False}
    positional: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        key, _, inline = arg.partition("=")
        if key in ("--report-file", "--page-title"):
            field = key.lstrip("-").replace("-", "_")
            if inline:
                opts[field] = inline
                i += 1
            elif i + 1 < len(argv):
                opts[field] = argv[i + 1]
                i += 2
            else:
                print(f"[upload] {key} needs a value")
                sys.exit(1)
            continue
        if key == "--new-page":
            opts["new_page"] = True
            i += 1
            continue
        positional.append(arg)
        i += 1
    return (positional[0] if positional else None), opts


def main() -> None:
    load_env()

    target, opts = _parse_args(sys.argv[1:])
    if not target:
        print(__doc__)
        sys.exit(1)

    out_dir = Path(target)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir

    page_title = opts["page_title"] or f"[UNSIGNED] {out_dir.name}"
    upload_report(
        out_dir,
        page_title,
        report_file=opts["report_file"],
        new_page=opts["new_page"],
    )


if __name__ == "__main__":
    main()
