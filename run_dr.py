"""
run_dr.py — standalone DR conditions runner.

Parses Leiden Cryogenics .dat files for a given date/time window and saves
dr_conditions.md to outputs/dr_<YYYYMMDD>/

Usage:
    python run_dr.py "2025-02-18"
    python run_dr.py "2025-02-18" --hours 24
    python run_dr.py "2025-02-18 14:00" "2025-02-18 22:00"

The output folder is printed on the last line so Claude can read it.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

from lab_agent.dr_conditions import get_dr_conditions

DR_DATA_PATH = os.getenv("DR_DATA_PATH", "")
OUTPUT_ROOT  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")


def parse_args():
    hours = 12
    skip_next = False
    clean = []
    for i, a in enumerate(sys.argv[1:], 1):
        if skip_next:
            skip_next = False
            continue
        if a == "--hours":
            if i + 1 >= len(sys.argv):
                print("ERROR: --hours requires a value (e.g. --hours 24)")
                sys.exit(1)
            hours = int(sys.argv[i + 1])
            skip_next = True
        elif not a.startswith("--"):
            clean.append(a)
    args = clean

    if len(args) == 0:
        print(__doc__)
        sys.exit(1)
    elif len(args) == 1:
        # Single date string — could be "2025-02-18" or "2025-02-18 14:00"
        raw = args[0]
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt), None, hours
            except ValueError:
                pass
        print(f"ERROR: could not parse date: {raw!r}")
        sys.exit(1)
    else:
        # Two args = explicit start and end — parse each independently
        start_str, end_str = args[0], args[1]
        start = end = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            if start is None:
                try:
                    start = datetime.strptime(start_str, fmt)
                except ValueError:
                    pass
            if end is None:
                try:
                    end = datetime.strptime(end_str, fmt)
                except ValueError:
                    pass
        if start is None or end is None:
            print(f"ERROR: could not parse date range: {start_str!r} {end_str!r}")
            sys.exit(1)
        return start, end, None


def main():
    if not DR_DATA_PATH:
        print("ERROR: DR_DATA_PATH is not set in .env")
        sys.exit(1)

    experiment_date, end_dt, hours = parse_args()

    if end_dt is not None:
        # Explicit start/end — pass bounds directly so the time components are preserved.
        md = get_dr_conditions(
            DR_DATA_PATH, experiment_date,
            explicit_start=experiment_date,
            explicit_end=end_dt,
        )
    else:
        md = get_dr_conditions(DR_DATA_PATH, experiment_date, window_hours=hours)

    if not md:
        print(f"No DR data found for {experiment_date.strftime('%Y-%m-%d')} "
              f"in {DR_DATA_PATH}")
        sys.exit(1)

    # Save to outputs/dr_YYYYMMDD/
    folder_name = f"dr_{experiment_date.strftime('%Y%m%d')}"
    out_dir = Path(OUTPUT_ROOT) / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dr_conditions.md").write_text(md, encoding="utf-8")

    print(f"[dr_runner] Saved dr_conditions.md")
    print(f"[dr_runner] Output folder: {out_dir}")


if __name__ == "__main__":
    main()
