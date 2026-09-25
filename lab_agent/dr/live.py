"""Recent dilution-refrigerator thermometry, read straight from the Leiden LogLCR files.

``conditions.py`` summarises a window for reports, from column positions and kelvin
limits fixed when it was written. The logs have moved on since: the thermometers were
swapped (TT-2450, then TT-2473, then TT-26006 on the MC), the software writes mK, and
on the 2026 logs that parser drops every mixing-chamber reading and files the MC
thermometer under "4K". It also reads every LogLCR file ever written, 2.5 GB, to
answer a question about the last two hours.

This module takes neither shortcut:

* **Labels come from each file's own header** (the ``Date(String)`` row), so a sensor
  swap cannot mislabel a channel. A file without that header is one of the one-row
  stubs the software leaves when a session starts; it is skipped, never guessed at.
* **Only files written during the window are opened.** The logs are append-only, so a
  file last modified before the window starts holds nothing in it.
* **A session's segments are merged.** The software writes several ``_N`` files in
  parallel, each scanning channels in turn, so one row holds some channels and NaN for
  the rest. The latest reading of a channel is its newest finite, positive value.

Values are returned as logged, in mK. Standard library only, and read-only: nothing
here writes a file.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path

LCR_GLOB = "LogLCR___*.dat"
# Other logs the software has written. None is current on this fridge's PC as of
# 2026-09; they are checked only so a caller can be told pressures are not available.
OTHER_LOG_GLOBS = ("logFP___*.dat", "logPT___*.dat", "LogLCR2___*.dat")

_SESSION_TS = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})")
_ROW_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _session_start(name: str) -> datetime | None:
    m = _SESSION_TS.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d-%H-%M-%S")
    except ValueError:
        return None


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime)


def _is_thermometer(label: str) -> bool:
    """Whether a header label names a temperature channel worth reporting.

    Empty and ``OPEN`` slots are unused inputs. ``C_still`` is the still's
    capacitance level gauge, logged in the T columns but not a temperature.
    """
    label = label.strip()
    return bool(label) and label.upper() != "OPEN" and not label.startswith("C_")


def _value(text: str) -> float | None:
    """A usable reading: finite and positive. The logs use NaN, ±Inf, 0 and negative
    numbers for channels that are unplugged, out of range or not scanned this row."""
    try:
        v = float(text)
    except ValueError:
        return None
    return v if math.isfinite(v) and v > 0 else None


def read_lcr_file(path: Path, start: datetime, end: datetime) -> dict[str, list[tuple[datetime, float]]] | None:
    """Readings per labelled thermometer in one LogLCR file, for rows in [start, end].

    Returns None when the file has no header row to take labels from.
    """
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    if len(lines) < 4:
        return None
    names = lines[2].rstrip("\r\n").split("\t")
    labels = lines[3].rstrip("\r\n").split("\t")
    if not labels or labels[0].strip() != "Date(String)":
        return None

    # T0..T9 are temperatures; the R columns before them are resistances.
    channels = [(i, labels[i].strip()) for i, n in enumerate(names)
                if re.fullmatch(r"T\d", n.strip()) and i < len(labels)
                and _is_thermometer(labels[i])]
    readings: dict[str, list[tuple[datetime, float]]] = {label: [] for _, label in channels}
    for line in lines[4:]:
        parts = line.rstrip("\r\n").split("\t")
        try:
            ts = datetime.strptime(parts[0], _ROW_TS_FORMAT)
        except ValueError:
            continue  # calibration-name row, or a partial line being written
        if ts < start or ts > end:
            continue
        for i, label in channels:
            if i < len(parts):
                v = _value(parts[i])
                if v is not None:
                    readings[label].append((ts, v))
    return readings


# A channel the bridge is not scanning keeps logging its last value. Real thermometry
# always wanders in the last digit, so this many identical readings means a held value.
FROZEN_MIN_SAMPLES = 100


def is_frozen(values: list[float]) -> bool:
    """Whether a channel logged one unchanging value: held, not measured."""
    return len(values) >= FROZEN_MIN_SAMPLES and min(values) == max(values)


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def collect_readings(root: Path, start: datetime, end: datetime, pattern: str = LCR_GLOB,
                     ) -> tuple[dict[str, list[tuple[datetime, float]]], list[str], list[str]]:
    """Every labelled thermometer's (time, mK) readings in [start, end], time-sorted.

    Opens only files that can hold rows in the window, merges a session's parallel
    segments, and skips header-less stubs. Returns (readings, files read, stubs skipped).
    """
    merged: dict[str, list[tuple[datetime, float]]] = {}
    files_read, stubs_skipped = [], []
    for f in sorted(root.glob(pattern)):
        began = _session_start(f.name)
        if began and began > end:
            continue
        if _mtime(f) < start:
            continue  # append-only: nothing written after the window began
        chunk = read_lcr_file(f, start, end)
        if chunk is None:
            stubs_skipped.append(f.name)
            continue
        files_read.append(f.name)
        for label, pts in chunk.items():
            merged.setdefault(label, []).extend(pts)
    for pts in merged.values():
        pts.sort()
    return merged, files_read, stubs_skipped


def recent_thermometry(data_path: str | Path, start: datetime, end: datetime) -> dict:
    """Summarise every labelled thermometer between ``start`` and ``end``.

    Returns plain data (no prose) so a caller can format it: per channel the latest
    reading and its time, and min / median / max / count over the window, all in mK;
    when the log was last written; which files were read or skipped; and the newest
    of the other logs, so a missing pressure log is visible rather than silent.
    """
    root = Path(data_path)
    if not root.is_dir():
        return {"found": False, "reason": f"DR data folder not found: {root}"}

    merged, files_read, stubs_skipped = collect_readings(root, start, end)

    channels = {}
    for label, pts in merged.items():
        if not pts:
            continue
        vals = [v for _, v in pts]
        last_ts, last_v = pts[-1]
        channels[label] = {
            "latest_mK": round(last_v, 3),
            "latest_at": last_ts.isoformat(sep=" "),
            "min_mK": round(min(vals), 3),
            "median_mK": round(median(vals), 3),
            "max_mK": round(max(vals), 3),
            "first_in_window_mK": round(pts[0][1], 3),
            "n": len(vals),
        }
        if is_frozen(vals):
            channels[label]["frozen"] = ("the same value in every row: most likely not being "
                                         "scanned, so this is a held value, not a measurement")

    newest = max((datetime.fromisoformat(c["latest_at"]) for c in channels.values()),
                 default=None)
    other_logs = {}
    for pattern in OTHER_LOG_GLOBS:
        found = sorted(root.glob(pattern), key=_mtime)
        other_logs[pattern.split("___")[0]] = (
            _mtime(found[-1]).isoformat(sep=" ", timespec="minutes") if found else None)

    return {
        "found": bool(channels),
        "window_start": start.isoformat(sep=" ", timespec="minutes"),
        "window_end": end.isoformat(sep=" ", timespec="minutes"),
        "newest_reading_at": newest.isoformat(sep=" ") if newest else None,
        "channels": channels,
        "files_read": files_read,
        "stub_files_skipped": stubs_skipped,
        "other_logs_last_written": other_logs,
    }
