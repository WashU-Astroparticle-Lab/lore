"""
Standalone DR conditions runner (invoked via the root-level run_dr.py shim).

Parses Leiden Cryogenics .dat files for a date or time window and saves dr_conditions.md
to the output folder it prints on its last line.

Usage:
    python run_dr.py "2025-02-18"                              # the day, +/-12 h
    python run_dr.py "2025-02-18" --hours 24                   # the day, +/-24 h
    python run_dr.py "2025-02-18 14:00" "2025-02-18 22:00"     # explicit start and end
    python run_dr.py "2025-02-17" "2025-02-19"                 # Feb 17 00:00 to Feb 19 23:59
    python run_dr.py "2025-02-18 14:00" --hours 6              # 14:00 to 20:00
    python run_dr.py --last 2                                  # the last 2 hours; prints the
                                                               # summary, saves nothing
    python run_dr.py "2026-10-29 13:00" "2026-10-29 19:00" --csv RuOx12980
                                                               # one thermometer as a CSV
                                                               # (a label, part of one if
                                                               # only one fits, e.g. "TT",
                                                               # or a channel, "5" / "T5")

A CSV holds one row per real measurement of that thermometer, at the time it was taken,
read from its own channel's log file; its name and column name the thermometer. Each time
is the fridge PC's local time with its UTC offset (2026-11-01T01:30:05-06:00), so the hour
repeated when the clocks go back is two distinct hours. The lines printed with it say
which channel and file it came from, how many readings it has, what was left out, how to
read the times, every channel in the log by the logger's numbering (from 0) and, for a
CMN, how it compares with the MC's resistive thermometer: put those in the reply.
"""
from __future__ import annotations

import csv
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import OUTPUT_ROOT, dr_data_path
from ..dr import get_dr_conditions
from ..dr.live import (BASE_MK, channel_series, clock_note, cmn_comparison, describe_times,
                       lab_time)

_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _parse_when(raw: str) -> tuple[datetime, bool] | None:
    """(datetime, whether a time of day was given), or None."""
    for fmt in _FORMATS:
        try:
            return datetime.strptime(raw, fmt), fmt != "%Y-%m-%d"
        except ValueError:
            pass
    return None


def _flag_value(argv: list[str], flag: str) -> str | None:
    if flag not in argv:
        return None
    i = argv.index(flag)
    if i + 1 >= len(argv):
        print(f"ERROR: {flag} requires a value")
        sys.exit(1)
    return argv[i + 1]


def parse_args(argv: list[str] | None = None, now: datetime | None = None) -> dict:
    """The run described by ``argv``: {mode, start, end, hours, csv, window_desc}.

    mode "day" is the calendar day padded by ``hours``; "window" is an explicit start/end;
    "last" is the last ``hours`` hours up to now.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    hours_raw, last_raw, csv_label = (_flag_value(argv, "--hours"), _flag_value(argv, "--last"),
                                      _flag_value(argv, "--csv"))
    skip = {v for v in (hours_raw, last_raw, csv_label) if v is not None}
    args = [a for a in argv if not a.startswith("--") and a not in skip]
    hours = float(hours_raw) if hours_raw is not None else 12.0
    hours = int(hours) if hours.is_integer() else hours

    if last_raw is not None:
        # Aware: a naive "now" in the hour the clocks go back would be read as the first one.
        end = now or lab_time(datetime.now(timezone.utc))
        n = float(last_raw)
        return {"mode": "last", "start": end - timedelta(hours=n), "end": end, "hours": n,
                "csv": csv_label, "window_desc": f"last {n:g} h"}
    if not args:
        print(__doc__)
        sys.exit(1)
    first = _parse_when(args[0])
    if first is None:
        print(f"ERROR: could not parse date: {args[0]!r}")
        sys.exit(1)
    if len(args) >= 2:
        second = _parse_when(args[1])
        if second is None:
            print(f"ERROR: could not parse date range: {args[0]!r} {args[1]!r}")
            sys.exit(1)
        end, end_has_time = second
        if not end_has_time:
            # "2025-02-17" "2025-02-19" means through Feb 19; it used to stop at its midnight.
            end = end + timedelta(days=1) - timedelta(seconds=1)
        return {"mode": "window", "start": first[0], "end": end, "hours": None,
                "csv": csv_label, "window_desc": "explicit"}
    start, has_time = first
    if has_time:
        # A start time with --hours is that many hours from it. The time used to be dropped
        # silently and the whole day reported instead.
        return {"mode": "window", "start": start, "end": start + timedelta(hours=hours),
                "hours": hours, "csv": csv_label, "window_desc": f"{hours:g} h from start"}
    return {"mode": "day", "start": start, "end": None, "hours": hours, "csv": csv_label,
            "window_desc": f"+/-{hours:g} h"}


def _folder_for(run: dict) -> Path:
    """dr_<YYYYMMDD> for a day; an explicit window gets its own folder, so two reports
    that start on the same day no longer overwrite each other."""
    if run["mode"] == "day":
        return Path(OUTPUT_ROOT) / f"dr_{run['start']:%Y%m%d}"
    return Path(OUTPUT_ROOT) / f"dr_{run['start']:%Y%m%d-%H%M}_{run['end']:%Y%m%d-%H%M}"


def _write_csv(data_path: str, run: dict) -> None:
    start, end = run["start"], run["end"]
    if end is None:  # a day: the whole day, padded like the summary
        day = datetime(start.year, start.month, start.day)
        start, end = (day - timedelta(hours=run["hours"]),
                      day + timedelta(hours=24 + run["hours"]) - timedelta(seconds=1))
    root = Path(data_path)
    series = channel_series(root, start, end, run["csv"])
    if series["ambiguous"]:
        print(f"[dr_csv] {run['csv']!r} fits more than one thermometer: "
              f"{_fits(series)}. Give the full label or the channel number.")
        sys.exit(1)
    if not series["files"]:
        print(f"[dr_csv] No log file for {run['csv']!r} between {start:%Y-%m-%d %H:%M} and "
              f"{end:%Y-%m-%d %H:%M}. Use a label from a log header (e.g. RuOx12980) or a "
              "channel number.")
        sys.exit(1)
    labels = sorted(series["labels"])
    chans = sorted(series["channels"])
    # The file and its column name the thermometer. They used to name only what was asked
    # for ("4_….csv", "temperature_mK"), so a CMN223 file passed as the TT-26006 for a week.
    name = "_".join(re.sub(r"[^A-Za-z0-9]+", "-", lab).strip("-") for lab in labels)
    out_dir = Path(OUTPUT_ROOT) / "dr_csv"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (f"ch{'-'.join(map(str, chans))}_{name}_"
                     f"{start:%Y%m%d-%H%M}_{end:%Y%m%d-%H%M}.csv")
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", f"{' / '.join(labels)} (mK)"])
        for ts, v in series["readings"]:
            w.writerow([ts.isoformat(timespec="seconds"), f"{v:.6g}"])
    left = ", ".join(f"{k} {r}" for r, k in sorted(series["dropped"].items())) or "nothing"
    print(f"[dr_csv] {', '.join(labels)}: channel {', '.join(map(str, chans))} "
          f"(column {', '.join(f'T{n}' for n in chans)}), "
          f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}")
    print(f"[dr_csv] {len(series['readings'])} readings, one per measurement, from "
          f"{', '.join(series['files'])}")
    if len(series["labels"]) > 1:
        print("[dr_csv] WARNING: the label changed between log sessions in this window; "
              "these are different sensors.")
    print(f"[dr_csv] Left out: {left}")
    if series["readings"]:
        print(f"[dr_csv] Times: {describe_times([t for t, _ in series['readings']])} Each "
              "timestamp ends in its UTC offset.")
    if clock_note(series["clock"]):
        print(f"[dr_csv] WARNING: {clock_note(series['clock'])}")
    print(f"[dr_csv] Channels in this log, numbered as the logger numbers them (from 0: file "
          f"_N.dat, column TN): {_channel_list(series['map'])}")
    for label in labels:
        note = _cmn_note(label, cmn_comparison(root, start, end, label, series["readings"]))
        if note:
            print(f"[dr_csv] NOTE: {note}")
    print(f"[dr_csv] CSV: {out}")


def _fits(series: dict) -> str:
    where = {lab: n for n, ch in series["map"].items() for lab in ch["labels"]}
    return ", ".join(f"{lab} (channel {where[lab]})" if lab in where else lab
                     for lab in series["ambiguous"])


def _channel_list(cmap: dict) -> str:
    def entry(n: int) -> str:
        return f"{n} {' / '.join(sorted(cmap[n]['labels']))}"
    measured = [entry(n) for n in sorted(cmap) if cmap[n]["measured"]]
    idle = [entry(n) for n in sorted(cmap) if not cmap[n]["measured"]]
    return (", ".join(measured) + " measured"
            + (f"; {', '.join(idle)} not measured" if idle else ""))


def _times(r: float) -> str:
    return f"{r:.1f}x" if r < 10 else f"{r:.0f}x"


def _cmn_note(label: str, cmp: dict | None) -> str | None:
    """What a CMN export needs said beside it, from cmn_comparison; None for other sensors."""
    if cmp is None:
        return None
    salt = f"{label} is a CMN salt thermometer, which reads correctly only near base."
    if cmp["reference"] is None or not cmp["paired"]:
        return f"{salt} No other MC thermometer was measured in this window to check it against."
    ref = f"{cmp['reference']} (channel {cmp['channel']})"
    if not cmp["off_base"]:
        return f"{salt} {ref} stayed below {BASE_MK:.0f} mK throughout, so the fridge was near base."
    said = (f"{salt} {ref} read above {BASE_MK:.0f} mK at {cmp['off_base']} of these "
            f"{cmp['paired']} readings; at those times {label} read "
            f"{_times(cmp['ratio_min'])} to {_times(cmp['ratio_max'])} what {cmp['reference']} "
            f"did (median {_times(cmp['ratio_median'])}).")
    if 2 / 3 <= cmp["ratio_median"] <= 1.5:
        return said
    return f"{said} For anything above base, use {ref}."


def main() -> None:
    data_path = dr_data_path()
    if not data_path:
        print("ERROR: DR_DATA_PATH is not set in .env")
        sys.exit(1)

    run = parse_args()
    if run["csv"]:
        _write_csv(data_path, run)
        return

    if run["mode"] == "day":
        md = get_dr_conditions(data_path, run["start"], window_hours=run["hours"])
    else:
        md = get_dr_conditions(data_path, run["start"], explicit_start=run["start"],
                               explicit_end=run["end"])
    if not md:
        print(f"No DR data found for {run['start']:%Y-%m-%d %H:%M} in {data_path}")
        sys.exit(1)

    if run["mode"] == "last":
        # A status check: print it. It used to be written to outputs/dr_<today>/, over the
        # dr_conditions.md of any report running that day.
        print(md)
        return

    out_dir = _folder_for(run)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dr_conditions.md").write_text(md, encoding="utf-8")
    print("[dr_runner] Saved dr_conditions.md")
    print(f"[dr_runner] Output folder: {out_dir}")


if __name__ == "__main__":
    main()
