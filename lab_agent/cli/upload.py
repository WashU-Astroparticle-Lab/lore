"""
Upload a finished experiment report to LabArchives (invoked via the root-level
upload_to_labarchives.py shim).

Usage:
    python upload_to_labarchives.py <out_dir>

Example:
    python upload_to_labarchives.py outputs/power_calibration_20260227

The page title in LabArchives is taken from the output folder name.
The report must already exist in <out_dir> (newest "[UNSIGNED] *.md", or
experiment_report.md as a fallback).
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..config import PROJECT_ROOT, load_env
from ..publish import upload_report


def main() -> None:
    load_env()

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    out_dir = Path(sys.argv[1])
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir

    page_title = f"[UNSIGNED] {out_dir.name}"
    upload_report(out_dir, page_title)


if __name__ == "__main__":
    main()
