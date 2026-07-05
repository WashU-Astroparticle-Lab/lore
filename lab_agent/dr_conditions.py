"""
dr_conditions.py — Parse Leiden Cryogenics dilution refrigerator .dat files.

Given a Data folder path and an experiment date, scans LogLCR and logFP files
for the surrounding time window and returns a markdown summary of temperatures
and pressures. Designed to be called from run.py after the main pipeline steps.

Usage:
    from lab_agent.dr_conditions import get_dr_conditions
    md = get_dr_conditions("/path/to/Data", experiment_date, window_hours=12)
    # Returns None if DR_DATA_PATH is not set or no data found for that window.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path


# LabVIEW epoch starts 1904-01-01; Unix epoch starts 1970-01-01.
# Not needed for parsing (we use Date1 string) but kept for reference.
_LABVIEW_OFFSET = 2_082_844_800  # seconds


# ── column positions in LogLCR files ──────────────────────────────────────────
# Header row 2 (0-indexed): Date1  Date2  R0..R9  T0..T9  I0..I3
# Row 3 maps column names to physical channel labels.
# We hardcode what we care about based on the known Leiden Cryogenics setup:

# LogLCR (primary unit) temperature columns → (index, label, valid_K_max)
_LCR_TEMP_COLS = [
    ("T2", "Still",        2.0),   # ~700 mK when cold
    ("T3", "4K (TT-2450)", 400.0), # 4 K stage
    ("T4", "MXC (CMN 172)", 1.0),  # mixing chamber ~10-50 mK base
    ("T5", "MXC (CMN LC09)", 1.0), # second MXC sensor
    ("T6", "50 mK plate",  0.5),   # 50 mK cold plate
]

# LogLCR2 (secondary unit) temperature columns → (col, label, valid_K_max)
_LCR2_TEMP_COLS = [
    ("T0", "TT-2453 (4K)", 400.0),
    ("T1", "TT-2458 (4K)", 400.0),
    ("T2", "TT-2460 (4K)", 400.0),
    ("T3", "TT-2461 (4K)", 400.0),
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_float(s: str) -> float | None:
    try:
        v = float(s)
        if v != v:  # NaN check without importing math
            return None
        if v == float("inf") or v == float("-inf"):
            return None
        return v
    except (ValueError, TypeError):
        return None


def _ts_from_filename(name: str) -> datetime | None:
    """Extract the session start datetime from a Leiden Cryogenics filename."""
    m = re.search(r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})", name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d-%H-%M-%S")
        except ValueError:
            pass
    return None


def _parse_lcr_file(
    path: Path,
    start: datetime,
    end: datetime,
    temp_cols: list[tuple[str, str, float]],
) -> dict[str, list[float]]:
    """
    Parse a LogLCR or LogLCR2 file, returning lists of valid temperature
    readings per channel for rows whose Date1 falls within [start, end].
    """
    result: dict[str, list[float]] = {label: [] for _, label, _ in temp_cols}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return result

    if len(lines) < 5:
        return result

    # Line index 2 (0-based) = column names
    col_names = lines[2].strip().split("\t")
    col_idx = {name.strip(): i for i, name in enumerate(col_names)}

    for line in lines[4:]:  # data starts at line 4
        parts = line.rstrip("\n").split("\t")
        if not parts or not parts[0]:
            continue
        try:
            ts = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < start or ts > end:
            continue
        for col, label, max_k in temp_cols:
            idx = col_idx.get(col)
            if idx is None or idx >= len(parts):
                continue
            v = _safe_float(parts[idx])
            if v is not None and 0 < v <= max_k:
                result[label].append(v)

    return result


def _parse_lcr_mxc_timestamps(
    path: Path,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, float]]:
    """
    Return (timestamp, mxc_K) pairs for every row in [start, end] where
    the MXC (T4, CMN 172) reading is valid (0 < T <= 1 K).
    Used for gap detection — needs timestamps, not just values.
    """
    result: list[tuple[datetime, float]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return result

    if len(lines) < 5:
        return result

    col_names = lines[2].strip().split("\t")
    col_idx = {name.strip(): i for i, name in enumerate(col_names)}
    mxc_idx = col_idx.get("T4")
    if mxc_idx is None:
        return result

    for line in lines[4:]:
        parts = line.rstrip("\n").split("\t")
        if not parts or not parts[0]:
            continue
        try:
            ts = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < start or ts > end:
            continue
        if mxc_idx >= len(parts):
            continue
        v = _safe_float(parts[mxc_idx])
        if v is not None and 0 < v <= 1.0:
            result.append((ts, v))

    return result


def _detect_mxc_gaps(
    timestamped: list[tuple[datetime, float]],
    gap_minutes: int = 5,
) -> list[tuple[datetime, datetime, int]]:
    """
    Given a sorted list of (timestamp, mxc_K) pairs, return a list of
    (gap_start, gap_end, gap_minutes) tuples where consecutive valid readings
    are separated by more than gap_minutes.
    """
    if len(timestamped) < 2:
        return []
    gaps = []
    sorted_ts = sorted(timestamped, key=lambda x: x[0])
    for i in range(1, len(sorted_ts)):
        delta = (sorted_ts[i][0] - sorted_ts[i - 1][0]).total_seconds() / 60
        if delta > gap_minutes:
            gaps.append((sorted_ts[i - 1][0], sorted_ts[i][0], round(delta)))
    return gaps


def _read_raw_mxc_during_gap(
    data_path: Path,
    gap_start: datetime,
    gap_end: datetime,
) -> list[tuple[datetime, float]]:
    """
    Scan LCR files for raw (unfiltered) MXC readings during a gap window.
    Returns all (timestamp, mxc_K) pairs regardless of temperature validity,
    so we can see what the MXC actually did during the event.
    """
    result: list[tuple[datetime, float]] = []
    for f in sorted(data_path.glob("LogLCR___*.dat")):
        file_ts = _ts_from_filename(f.name)
        if file_ts and file_ts > gap_end:
            continue
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        if len(lines) < 5:
            continue
        col_names = lines[2].strip().split("\t")
        col_idx   = {name.strip(): i for i, name in enumerate(col_names)}
        mxc_idx   = col_idx.get("T4")
        if mxc_idx is None:
            continue
        for line in lines[4:]:
            parts = line.rstrip("\n").split("\t")
            if not parts or not parts[0]:
                continue
            try:
                ts = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            # Only rows strictly inside the gap
            if ts <= gap_start or ts >= gap_end:
                continue
            if mxc_idx >= len(parts):
                continue
            v = _safe_float(parts[mxc_idx])
            # Accept any positive, finite value — no upper limit filter
            if v is not None and v > 0:
                result.append((ts, v))
    return sorted(result, key=lambda x: x[0])


def _parse_fp_file(
    path: Path,
    start: datetime,
    end: datetime,
) -> dict[str, list[float]]:
    """
    Parse a logFP file, returning pressure readings (P1–P4) within [start, end].
    """
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
        if not parts or not parts[0]:
            continue
        try:
            ts = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < start or ts > end:
            continue
        for col in ("P1", "P2", "P3", "P4"):
            idx = col_idx.get(col)
            if idx is None or idx >= len(parts):
                continue
            v = _safe_float(parts[idx])
            if v is not None:
                result[col].append(v)

    return result


def _stats(vals: list[float]) -> tuple[float, float, float] | None:
    if not vals:
        return None
    vals_sorted = sorted(vals)
    n = len(vals_sorted)
    median = vals_sorted[n // 2] if n % 2 else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2
    return min(vals_sorted), median, max(vals_sorted)


def _fmt(v: float, mK: bool = False) -> str:
    if mK:
        v *= 1000
        return f"{v:.1f} mK"
    if v < 2.0:
        return f"{v * 1000:.1f} mK"
    if v < 10:
        return f"{v:.3f} K"
    return f"{v:.1f} K"


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
        start = explicit_start
        end = explicit_end
    else:
        # Anchor to the calendar day: window extends window_hours before midnight
        # and window_hours after the end of the day, covering the whole day ± padding.
        start = datetime(experiment_date.year, experiment_date.month, experiment_date.day) - timedelta(hours=window_hours)
        end   = datetime(experiment_date.year, experiment_date.month, experiment_date.day, 23, 59, 59) + timedelta(hours=window_hours)

    # Collect temperatures from LogLCR (primary)
    lcr_temps: dict[str, list[float]] = {label: [] for _, label, _ in _LCR_TEMP_COLS}
    lcr_sample_count = 0
    mxc_timestamped: list[tuple[datetime, float]] = []
    for f in sorted(data_path.glob("LogLCR___*.dat")):
        file_ts = _ts_from_filename(f.name)
        if file_ts and file_ts > end:
            continue  # file started after window; skip
        chunk = _parse_lcr_file(f, start, end, _LCR_TEMP_COLS)
        for label, vals in chunk.items():
            lcr_temps[label].extend(vals)
        # Count total valid readings across all channels as sample count proxy
        if chunk:
            lcr_sample_count += max(len(v) for v in chunk.values())
        # Collect timestamped MXC readings for gap detection
        mxc_timestamped.extend(_parse_lcr_mxc_timestamps(f, start, end))

    # Collect temperatures from LogLCR2 (secondary — 4K TT sensors)
    lcr2_temps: dict[str, list[float]] = {label: [] for _, label, _ in _LCR2_TEMP_COLS}
    for f in sorted(data_path.glob("LogLCR2___*.dat")):
        file_ts = _ts_from_filename(f.name)
        if file_ts and file_ts > end:
            continue
        chunk = _parse_lcr_file(f, start, end, _LCR2_TEMP_COLS)
        for label, vals in chunk.items():
            lcr2_temps[label].extend(vals)

    # Collect pressures
    fp_data: dict[str, list[float]] = {f"P{i}": [] for i in range(1, 5)}
    fp_sample_count = 0
    for f in sorted(data_path.glob("logFP___*.dat")):
        file_ts = _ts_from_filename(f.name)
        if file_ts and file_ts > end:
            continue
        chunk = _parse_fp_file(f, start, end)
        for col, vals in chunk.items():
            fp_data[col].extend(vals)
        fp_sample_count += len(chunk.get("P1", []))

    # Check if we found anything
    any_temp = any(v for v in lcr_temps.values()) or any(v for v in lcr2_temps.values())
    any_pressure = any(v for v in fp_data.values())
    if not any_temp and not any_pressure:
        return None

    # ── Build markdown ────────────────────────────────────────────────────────
    lines = [
        "# Dilution Refrigerator Conditions",
        "",
        f"**Experiment date:** {experiment_date.strftime('%Y-%m-%d')}",
        f"**Window:** {start.strftime('%Y-%m-%d %H:%M')} to {end.strftime('%Y-%m-%d %H:%M')} (+/-{window_hours} h)",
        f"**Thermometry samples:** {lcr_sample_count}",
        f"**Pressure samples:** {fp_sample_count}",
    ]

    # Temperatures table
    if any_temp:
        lines += [
            "",
            "## Temperatures",
            "",
            "| Channel | Min | Median | Max | N |",
            "|---------|-----|--------|-----|---|",
        ]
        for _, label, max_k in _LCR_TEMP_COLS:
            vals = lcr_temps[label]
            s = _stats(vals)
            if s:
                lo, med, hi = s
                lines.append(f"| {label} | {_fmt(lo)} | {_fmt(med)} | {_fmt(hi)} | {len(vals)} |")
            else:
                lines.append(f"| {label} | — | — | — | 0 |")

        for _, label, max_k in _LCR2_TEMP_COLS:
            vals = lcr2_temps[label]
            s = _stats(vals)
            if s:
                lo, med, hi = s
                lines.append(f"| {label} | {_fmt(lo)} | {_fmt(med)} | {_fmt(hi)} | {len(vals)} |")

        # Highlight the most important number: MXC base temperature in mK
        mxc_vals = lcr_temps.get("MXC (CMN 172)", [])
        cold_mxc = [v for v in mxc_vals if v < 0.3]  # only sub-300 mK readings
        if cold_mxc:
            s = _stats(cold_mxc)
            if s:
                lo, med, hi = s
                lines += [
                    "",
                    f"> **MXC base temperature (sub-300 mK readings only):** "
                    f"min {_fmt(lo)}, median {_fmt(med)}, max {_fmt(hi)} "
                    f"({len(cold_mxc)} samples)",
                ]

        # Gap detection — flag interruptions in sub-1K MXC readings
        gaps = _detect_mxc_gaps(mxc_timestamped, gap_minutes=5)
        if gaps:
            lines += ["", "## Thermal Event Warnings"]
            lines.append("")
            lines.append(
                "Gaps of >5 minutes were detected between consecutive valid sub-1 K "
                "MXC readings. The raw data during each gap was inspected to show "
                "what the MXC actually did while it was outside the calibrated range."
            )
            for gap_start, gap_end, gap_min in gaps:
                lines += ["", f"### Gap: {gap_start.strftime('%Y-%m-%d %H:%M')} to {gap_end.strftime('%Y-%m-%d %H:%M')} ({gap_min} min)"]

                raw = _read_raw_mxc_during_gap(data_path, gap_start, gap_end)

                if not raw:
                    lines.append("No raw readings found during this gap (possible logging interruption).")
                else:
                    peak_ts, peak_v = max(raw, key=lambda x: x[1])
                    first_ts, first_v = raw[0]
                    last_ts,  last_v  = raw[-1]

                    lines.append(f"- **Readings during gap:** {len(raw)}")
                    lines.append(f"- **Peak MXC temperature:** {_fmt(peak_v)} at {peak_ts.strftime('%H:%M:%S')}")
                    lines.append(f"- **At gap start:** {_fmt(first_v)} ({first_ts.strftime('%H:%M:%S')})")
                    lines.append(f"- **At gap end:** {_fmt(last_v)} ({last_ts.strftime('%H:%M:%S')})")

                    # Characterise the event
                    if peak_v < 0.1:
                        severity = "minor fluctuation — MXC stayed sub-100 mK throughout"
                    elif peak_v < 1.0:
                        severity = "moderate — MXC warmed within the sub-1 K range but did not leave it"
                    elif peak_v < 4.0:
                        severity = "significant — MXC exceeded 1 K, quasiparticle density substantially elevated during this period"
                    elif peak_v < 10.0:
                        severity = "severe — MXC reached the 4 K stage temperature range, full cooldown recovery required"
                    else:
                        severity = "critical — MXC reached warm temperatures, system effectively lost base"
                    lines.append(f"- **Assessment:** {severity}")

    # Pressures table
    if any_pressure:
        lines += [
            "",
            "## Pressures",
            "",
            "| Channel | Min (mbar) | Median (mbar) | Max (mbar) | N |",
            "|---------|-----------|---------------|-----------|---|",
        ]
        pressure_labels = {
            "P1": "P1 (fore-line)",
            "P2": "P2",
            "P3": "P3",
            "P4": "P4",
        }
        for col, label in pressure_labels.items():
            vals = fp_data[col]
            s = _stats(vals)
            if s:
                lo, med, hi = s
                lines.append(f"| {label} | {lo:.3g} | {med:.3g} | {hi:.3g} | {len(vals)} |")

    return "\n".join(lines) + "\n"
