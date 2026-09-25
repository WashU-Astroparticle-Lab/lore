"""
lab_agent/dr/conditions.py — Parse Leiden Cryogenics dilution refrigerator .dat files.

Given a Data folder path and an experiment date, scans the LogLCR (and, where they are
still written, logFP) files for the surrounding time window and returns a markdown
summary of temperatures and pressures. Called from run_dr.py and from the experiment
pipeline (Step 2b).

Usage:
    from lab_agent.dr import get_dr_conditions
    md = get_dr_conditions("/path/to/Data", experiment_date, window_hours=12)
    # Returns None if DR_DATA_PATH is not set or no data found for that window.

Thermometry is read through ``live.collect_readings``: channel names come from each
file's own header and values are in mK, as the Leiden software writes them. This file
used to hardcode column positions (T3 = "4K (TT-2450)", T4 = "MXC (CMN 172)") and kelvin
limits. The logs were in mK even then, and two thermometer swaps later every
mixing-chamber reading was being dropped as out of range while TT-26006 [MC] at 47 mK
was reported as the 4K stage at 47 K. See ``live.py`` for how the files are read.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from .live import collect_readings, is_frozen, median

# Any MC-labelled reading below this is taken to mean the fridge is at base.
_BASE_MK = 300.0
# Consecutive sub-1 K MC readings further apart than this are a thermal event.
_GAP_MINUTES = 5
# Channels on the mixing chamber carry "[MC" in their header label ("[MC]", "[MC P]").
_MC_TAG = "[MC"


def _ts_from_filename(name: str) -> datetime | None:
    """Extract the session start datetime from a Leiden Cryogenics filename."""
    m = re.search(r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})", name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d-%H-%M-%S")
        except ValueError:
            pass
    return None


def _safe_float(s: str) -> float | None:
    try:
        v = float(s)
    except (ValueError, TypeError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _parse_fp_file(path: Path, start: datetime, end: datetime) -> dict[str, list[float]]:
    """Parse a logFP file, returning pressure readings (P1–P4) within [start, end]."""
    result: dict[str, list[float]] = {f"P{i}": [] for i in range(1, 5)}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return result
    if len(lines) < 2:
        return result

    col_names = lines[0].lstrip("#").strip().split("\t")
    col_idx = {name.strip(): i for i, name in enumerate(col_names)}
    for line in lines[1:]:
        parts = line.rstrip("\n").split("\t")
        try:
            ts = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < start or ts > end:
            continue
        for col in ("P1", "P2", "P3", "P4"):
            idx = col_idx.get(col)
            if idx is not None and idx < len(parts):
                v = _safe_float(parts[idx])
                if v is not None:
                    result[col].append(v)
    return result


def _fmt_mk(v: float) -> str:
    """A temperature given in mK, in the unit a person would say it in."""
    if v < 2000:
        return f"{v:.1f} mK"
    if v < 10000:
        return f"{v / 1000:.3f} K"
    return f"{v / 1000:.1f} K"


def _gap_channel(labels: list[str]) -> str | None:
    """The MC thermometer to watch for thermal events.

    A resistance thermometer ("TT-…", "RuOx…") reads across the whole range, so it is
    preferred; a CMN salt thermometer only reads at base and falls silent when the
    fridge warms, which is the event itself.
    """
    mc = [label for label in labels if _MC_TAG in label]
    resistive = [label for label in mc if "CMN" not in label.upper()]
    return (resistive or mc or [None])[0]


def _detect_gaps(points: list[tuple[datetime, float]]) -> list[tuple[datetime, datetime, int]]:
    """Gaps longer than _GAP_MINUTES between consecutive sub-1 K readings."""
    cold = [ts for ts, v in points if v < 1000]
    gaps = []
    for a, b in zip(cold, cold[1:]):
        minutes = (b - a).total_seconds() / 60
        if minutes > _GAP_MINUTES:
            gaps.append((a, b, round(minutes)))
    return gaps


def _severity(peak_mk: float) -> str:
    if peak_mk < 100:
        return "minor fluctuation — MXC stayed sub-100 mK throughout"
    if peak_mk < 1000:
        return "moderate — MXC warmed within the sub-1 K range but did not leave it"
    if peak_mk < 4000:
        return ("significant — MXC exceeded 1 K, quasiparticle density substantially "
                "elevated during this period")
    if peak_mk < 10000:
        return "severe — MXC reached the 4 K stage temperature range, full cooldown recovery required"
    return "critical — MXC reached warm temperatures, system effectively lost base"


# ── public API ────────────────────────────────────────────────────────────────

def experiment_date_from_id(experiment_id: str) -> datetime | None:
    """
    Extract experiment date from an ID like 'power_calibration_20260227'.
    Returns a datetime at midnight, or None if no 8-digit date found.
    """
    m = re.search(r"(\d{8})", experiment_id)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d")
        except ValueError:
            pass
    return None


def get_dr_conditions(
    data_path: str | Path,
    experiment_date: datetime,
    window_hours: int = 12,
    *,
    explicit_start: datetime | None = None,
    explicit_end: datetime | None = None,
) -> str | None:
    """
    Scan Leiden Cryogenics .dat files in data_path and return a Markdown summary
    of DR conditions around experiment_date ± window_hours.

    If explicit_start and explicit_end are given, they are used directly as the
    window bounds (experiment_date is still used for the report header).

    Returns None if data_path doesn't exist or no rows fall in the window.
    """
    data_path = Path(data_path)
    if not data_path.exists():
        return None

    if explicit_start is not None and explicit_end is not None:
        start, end = explicit_start, explicit_end
        window_desc = "explicit"
    else:
        # Anchor to the calendar day: the whole day, padded by window_hours each side.
        day = datetime(experiment_date.year, experiment_date.month, experiment_date.day)
        start = day - timedelta(hours=window_hours)
        end = day + timedelta(hours=24 + window_hours) - timedelta(seconds=1)
        window_desc = f"+/-{window_hours} h"

    temps, _, _ = collect_readings(data_path, start, end)
    secondary, _, _ = collect_readings(data_path, start, end, pattern="LogLCR2___*.dat")
    for label, pts in secondary.items():
        temps.setdefault(f"{label} (LCR2)", []).extend(pts)
    temps = {label: pts for label, pts in temps.items() if pts}

    fp_data: dict[str, list[float]] = {f"P{i}": [] for i in range(1, 5)}
    for f in sorted(data_path.glob("logFP___*.dat")):
        file_ts = _ts_from_filename(f.name)
        if file_ts and file_ts > end:
            continue
        if datetime.fromtimestamp(f.stat().st_mtime) < start:
            continue  # append-only log, last written before the window
        chunk = _parse_fp_file(f, start, end)
        for col, vals in chunk.items():
            fp_data[col].extend(vals)
    any_pressure = any(fp_data.values())

    if not temps and not any_pressure:
        return None

    samples = len({ts for pts in temps.values() for ts, _ in pts})
    lines = [
        "# Dilution Refrigerator Conditions",
        "",
        f"**Experiment date:** {experiment_date.strftime('%Y-%m-%d')}",
        f"**Window:** {start.strftime('%Y-%m-%d %H:%M')} to {end.strftime('%Y-%m-%d %H:%M')} ({window_desc})",
        f"**Thermometry samples:** {samples}",
        f"**Pressure samples:** {len(fp_data['P1'])}",
        "",
        "Channel names are taken from the log file headers; values are logged in mK.",
    ]

    if temps:
        lines += [
            "",
            "## Temperatures",
            "",
            "| Channel | Min | Median | Max | Latest | N |",
            "|---------|-----|--------|-----|--------|---|",
        ]
        frozen = []
        for label, pts in temps.items():
            vals = [v for _, v in pts]
            if is_frozen(vals):
                frozen.append(label)
            lines.append(f"| {label}{' (held value)' if label in frozen else ''} | "
                         f"{_fmt_mk(min(vals))} | {_fmt_mk(median(vals))} | "
                         f"{_fmt_mk(max(vals))} | {_fmt_mk(pts[-1][1])} "
                         f"({pts[-1][0].strftime('%m-%d %H:%M')}) | {len(vals)} |")
        if frozen:
            lines += ["", "**Held values:** " + ", ".join(frozen) + " logged one unchanging "
                      "value for the whole window. The bridge was most likely not scanning "
                      "these channels, so the number is the last reading before the window, "
                      "not a measurement during it. Do not use it as the stage's temperature."]

        # The most important numbers: each MC thermometer at base.
        for label, pts in temps.items():
            if _MC_TAG not in label or label in frozen:
                continue
            base = [v for _, v in pts if v < _BASE_MK]
            if base:
                lines += ["", f"> **MXC base temperature, {label} (sub-300 mK readings only):** "
                              f"min {_fmt_mk(min(base))}, median {_fmt_mk(median(base))}, "
                              f"max {_fmt_mk(max(base))} ({len(base)} samples)"]

        watched = _gap_channel([label for label in temps if label not in frozen])
        gaps = _detect_gaps(temps[watched]) if watched else []
        if gaps:
            lines += [
                "", "## Thermal Event Warnings", "",
                f"Gaps of >{_GAP_MINUTES} minutes were detected between consecutive sub-1 K "
                f"readings of {watched}. The readings during each gap show what the MXC did "
                "while it was above 1 K.",
            ]
            for gap_start, gap_end, gap_min in gaps:
                lines += ["", f"### Gap: {gap_start.strftime('%Y-%m-%d %H:%M')} to "
                              f"{gap_end.strftime('%Y-%m-%d %H:%M')} ({gap_min} min)"]
                raw = [(ts, v) for ts, v in temps[watched] if gap_start < ts < gap_end]
                if not raw:
                    lines.append("No readings found during this gap (possible logging interruption).")
                    continue
                peak_ts, peak_v = max(raw, key=lambda x: x[1])
                lines += [
                    f"- **Readings during gap:** {len(raw)}",
                    f"- **Peak MXC temperature:** {_fmt_mk(peak_v)} at {peak_ts.strftime('%H:%M:%S')}",
                    f"- **At gap start:** {_fmt_mk(raw[0][1])} ({raw[0][0].strftime('%H:%M:%S')})",
                    f"- **At gap end:** {_fmt_mk(raw[-1][1])} ({raw[-1][0].strftime('%H:%M:%S')})",
                    f"- **Assessment:** {_severity(peak_v)}",
                ]

    if any_pressure:
        lines += [
            "",
            "## Pressures",
            "",
            "| Channel | Min (mbar) | Median (mbar) | Max (mbar) | N |",
            "|---------|-----------|---------------|-----------|---|",
        ]
        labels = {"P1": "P1 (fore-line)", "P2": "P2", "P3": "P3", "P4": "P4"}
        for col, label in labels.items():
            vals = fp_data[col]
            if vals:
                lines.append(f"| {label} | {min(vals):.3g} | {median(vals):.3g} | "
                             f"{max(vals):.3g} | {len(vals)} |")
    else:
        lines += ["", "## Pressures", "",
                  "No pressure log (logFP) was written during this window, so pressures "
                  "and pump state are unknown. This is a gap in logging, not a reading."]

    return "\n".join(lines) + "\n"
