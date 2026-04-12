"""
upload_to_labarchives.py — upload a finished experiment report to LabArchives.

Usage:
    python upload_to_labarchives.py <out_dir>

Example:
    python upload_to_labarchives.py outputs/power_calibration_20260227

The page title in LabArchives is taken from the output folder name.
The report must already exist at <out_dir>/experiment_report.md.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

from lab_agent.upload import upload_report

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    out_dir = Path(sys.argv[1])
    if not out_dir.is_absolute():
        out_dir = Path(os.path.dirname(os.path.abspath(__file__))) / out_dir

    page_title = f"[UNSIGNED] {out_dir.name}"
    upload_report(out_dir, page_title)
