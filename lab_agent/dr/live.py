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
* **Each channel is read only from its own file.** The bridge measures one channel at a
  time, and the software writes one file per channel: ``_5.dat`` gets a row each time
  channel 5 is measured. That row also carries every other T column, repeating each
  one's last reading under this row's time. Reading every column of every file counted
  each measurement 3-4 times, stamped up to 15 s after it was taken: 4,072 RuOx rows for
  an afternoon that had 1,251 RuOx measurements. A thermometer with no file of its own in
  the window was not being scanned, and is listed with no readings rather than with
  copies of an old one.
* **Times are real elapsed time.** Each row carries its time twice: Date1 is the PC's
  wall clock, Date2 the absolute time. Date1 alone is wrong twice a year: on 2026-11-01
  it ran from 01:00 to 01:59 twice (CDT, then CST), and on 2026-03-08 it jumped from 01:59
  to 03:00, which read as an hour with no data. Every time returned here is the wall-clock
  time with the UTC offset it was logged under, taken from Date2, so the repeated hour
  sorts in order and the skipped one is no gap. A row whose two times disagree with the
  lab's time zone is counted (``clock``), so a PC clock set wrong is reported, not hidden.

Values are returned as logged, in mK. Standard library only, and read-only: nothing
here writes a file.
"""
from __future__ import annotations

import bisect
import functools
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

LCR_GLOB = "LogLCR___*.dat"
# Other logs the software has written. None is current on this fridge's PC as of
# 2026-09; they are checked only so a caller can be told pressures are not available.
OTHER_LOG_GLOBS = ("logFP___*.dat", "logPT___*.dat", "LogLCR2___*.dat")

_SESSION_TS = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})")
_FILE_CHANNEL = re.compile(r"_(\d)\.dat$", re.IGNORECASE)
_ROW_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_ROW_TS = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\t")
# Date2 is LabVIEW's clock: seconds since 1904-01-01 00:00 UTC.
_LABVIEW_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


# ── time ──────────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def _zone_named(name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


@functools.lru_cache(maxsize=1)
def _configured_zone_name() -> str:
    try:
        from ..config import lab_config_value
    except ImportError:
        return ""
    return lab_config_value("Time zone")


def lab_zone():
    """The fridge PC's time zone: LAB_TIMEZONE, else lab_config.md's "Time zone" (an IANA
    name such as America/Chicago), else None, meaning this computer's own zone. A name that
    does not load also gives None; on Windows the zone data is the tzdata package."""
    name = os.environ.get("LAB_TIMEZONE") or _configured_zone_name()
    return _zone_named(name) if name else None


_FIXED: dict = {}


def _fixed(offset: timedelta, name: str | None = None) -> timezone:
    if name and " " in name:  # Windows names its zones in full: "Central Daylight Time"
        name = "".join(w[0] for w in name.split())
    key = (offset, name)
    if key not in _FIXED:
        _FIXED[key] = timezone(offset, name) if name else timezone(offset)
    return _FIXED[key]


def lab_time(dt: datetime) -> datetime:
    """``dt`` on the fridge PC's clock, with the UTC offset in force then fixed on it.

    A naive ``dt`` is read as that clock's wall time; in the hour the clocks go back it is
    the first of the two. The offset is fixed (``timezone(-6 h, "CST")``) rather than a
    zone because Python compares two times in the *same* zone by wall time, which sorts
    01:30 CST before 01:45 CDT although it came 45 minutes after it.
    """
    zone = lab_zone()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone) if zone else dt.astimezone()
    else:
        dt = dt.astimezone(zone) if zone else dt.astimezone()
    return dt.replace(tzinfo=_fixed(dt.utcoffset(), dt.tzname()))


def _logged_offset(wall: datetime, date2: str | None) -> timedelta | None:
    """The UTC offset a row was logged under, Date1 minus Date2; None when Date2 is not a
    usable absolute time (it must agree with Date1 to a whole quarter hour, within 14 h)."""
    if not date2:
        return None
    try:
        utc = _LABVIEW_EPOCH + timedelta(seconds=float(date2))
    except (ValueError, OverflowError):
        return None
    diff = (wall - utc.replace(tzinfo=None)).total_seconds()
    quarter = round(diff / 900) * 900
    if abs(diff - quarter) > 5 or abs(quarter) > 14 * 3600:
        return None
    return timedelta(seconds=quarter)


def _row_time(wall: datetime, date2: str | None) -> tuple[datetime, timedelta | None]:
    """(the row's time, the offset the lab's zone gives for it when the log's differs)."""
    offset = _logged_offset(wall, date2)
    if offset is None:
        return lab_time(wall), None  # no absolute time to go by: the wall clock
    ts = wall.replace(tzinfo=_fixed(offset))
    local = lab_time(ts)
    if local.utcoffset() == offset:
        return local, None  # the same instant, with the zone's name for it ("CDT")
    return ts, local.utcoffset()


def new_clock() -> dict:
    """A tally for ``read_lcr_file(clock=...)``: rows read, and rows whose wall-clock time
    (Date1) is not what the lab's time zone makes of their absolute time (Date2)."""
    return {"rows": 0, "mismatched": 0, "first": None, "logged": set(), "expected": set()}


def _utc(offset: timedelta) -> str:
    minutes = round(offset.total_seconds() / 60)
    sign = "-" if minutes < 0 else "+"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def clock_note(clock: dict | None) -> str | None:
    """What a reader must be told when the log's two times disagree; None when they agree."""
    if not clock or not clock["mismatched"]:
        return None
    logged = " / ".join(_utc(o) for o in sorted(clock["logged"]))
    expected = " / ".join(_utc(o) for o in sorted(clock["expected"]))
    return (f"{clock['mismatched']} of {clock['rows']} readings were logged with the PC's "
            f"clock at {logged}, where the lab's time zone gives {expected} (first at "
            f"{clock['first']:%Y-%m-%d %H:%M}). The PC's clock or time-zone setting was "
            "off. Those readings keep the offset they were logged under, so their order "
            "and spacing are right, but their clock time may be off by the difference. "
            "Check the PC's date and time settings.")


def _zone_label(t: datetime) -> str:
    name = t.tzname() or ""
    return name if name.startswith("UTC") else f"{name} ({_utc(t.utcoffset())})"


def describe_times(times: list[datetime]) -> str:
    """How to read a series' clock times: the zone they are in, and any change of clocks
    inside the series. ``times`` must be sorted."""
    if not times:
        return ""
    parts = [_zone_label(times[0])]
    for a, b in zip(times, times[1:]):
        step = b.utcoffset() - a.utcoffset()
        if not step:
            continue
        wall = b.replace(tzinfo=None)
        if step < timedelta(0):
            what = (f"the clocks went back, so {wall:%H}:00-{wall:%H}:59 on {wall:%Y-%m-%d} "
                    "appears twice; the UTC offset on each time tells the two apart")
        else:
            gone = wall - step
            what = (f"the clocks went forward, so no reading is stamped {gone:%H}:00-"
                    f"{gone:%H}:59 on {wall:%Y-%m-%d}, and none is missing")
        parts.append(f"then {_zone_label(b)} from {wall:%Y-%m-%d %H:%M:%S} ({what})")
    return "the fridge PC's local time, " + ", ".join(parts) + "."


def file_channel(name: str) -> int | None:
    """The bridge channel a LogLCR file belongs to: ``..._5.dat`` -> 5, else None."""
    m = _FILE_CHANNEL.search(name)
    return int(m.group(1)) if m else None


def _session_start(name: str) -> datetime | None:
    m = _SESSION_TS.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d-%H-%M-%S")
    except ValueError:
        return None


def modified_at(path: Path) -> datetime:
    """When ``path`` was last written, on the lab's clock (see ``lab_time``)."""
    return lab_time(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))


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


# Platinum sensors (PT100/PT1000) are calibrated down to tens of kelvin. Below that their
# resistance barely changes and the calibration extrapolates to nonsense: the "MC Plate"
# PT1000 logged 60-574 mK while the mixing chamber's own thermometer read 7-15 K.
PLATINUM_MIN_MK = 20_000.0


def _uncalibrated(t_text: str, r_text: str) -> bool:
    """Whether a T cell just repeats the R cell: the channel had no calibration.

    The software then copies the resistance into the temperature column, so 2.2 kOhm was
    read as 2.2 K. It is checked per row, not from the header, because a calibration can be
    switched on mid-file: on 2026-07-27 the header said "No Calibration" and from 15:13 the
    column held real temperatures (12,895 Ohm next to 76 mK).
    """
    try:
        t, r = float(t_text), float(r_text)
    except ValueError:
        return False
    return math.isfinite(r) and abs(t - r) <= 1e-4 * max(abs(r), 1.0)


def read_lcr_file(path: Path, start: datetime, end: datetime, channel: int | None = None,
                  dropped: dict | None = None, clock: dict | None = None,
                  ) -> dict[str, list[tuple[datetime, float]]] | None:
    """Readings per labelled thermometer in one LogLCR file, for rows in [start, end].

    With ``channel`` (the file's ``_N``), only column T<channel> is read: the other T
    columns are copies of earlier readings. Every thermometer in the header is still a
    key, with an empty list, so a caller can tell "not measured here" from "not wired".
    Without it, every column is read.

    A reading is left out when it is not finite and positive, when the T column only
    repeats the resistance (no calibration), or when a platinum sensor reads below its
    range. ``dropped``, if given, counts what was left out: {label: {reason: n}}.

    Times are aware (see ``lab_time``); a naive ``start`` or ``end`` is read as the lab's
    wall clock. ``clock``, a ``new_clock()``, tallies rows whose Date1 and Date2 disagree.

    Returns None when the file has no header row to take labels from.
    """
    start, end = lab_time(start), lab_time(end)
    # A cheap first cut on the wall clock; no UTC offset is more than 14 h.
    lo = start.replace(tzinfo=None) - timedelta(hours=15)
    hi = end.replace(tzinfo=None) + timedelta(hours=15)
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    if len(lines) < 4:
        return None
    names = [n.strip() for n in lines[2].rstrip("\r\n").split("\t")]
    labels = lines[3].rstrip("\r\n").split("\t")
    if not labels or labels[0].strip() != "Date(String)":
        return None
    # The row after the labels names each column's calibration ("RuOx12980", "PT1000",
    # "No Calibration"), when the file has one.
    calibrations = []
    if len(lines) > 4 and not _ROW_TS.match(lines[4]):
        calibrations = lines[4].rstrip("\r\n").split("\t")

    # T0..T9 are temperatures; the R columns before them are resistances.
    channels = []
    for i, n in enumerate(names):
        if re.fullmatch(r"T\d", n) and i < len(labels) and _is_thermometer(labels[i]):
            r = names.index(f"R{n[1:]}") if f"R{n[1:]}" in names else None
            cal = calibrations[i].strip() if i < len(calibrations) else ""
            channels.append((i, labels[i].strip(), r, "PT10" in cal.upper()))
    readings: dict[str, list[tuple[datetime, float]]] = {c[1]: [] for c in channels}
    if channel is not None:
        channels = [c for c in channels if names[c[0]] == f"T{channel}"]

    def drop(label: str, reason: str) -> None:
        if dropped is not None:
            counts = dropped.setdefault(label, {})
            counts[reason] = counts.get(reason, 0) + 1

    date2 = names.index("Date2") if "Date2" in names else None
    for line in lines[4:]:
        parts = line.rstrip("\r\n").split("\t")
        try:
            wall = datetime.strptime(parts[0], _ROW_TS_FORMAT)
        except ValueError:
            continue  # calibration-name row, or a partial line being written
        if wall < lo or wall > hi:
            continue
        ts, expected = _row_time(wall, parts[date2] if date2 is not None and date2 < len(parts)
                                 else None)
        if ts < start or ts > end:
            continue
        if clock is not None:
            clock["rows"] += 1
            if expected is not None:
                clock["mismatched"] += 1
                clock["first"] = min(clock["first"] or ts, ts)
                clock["logged"].add(ts.utcoffset())
                clock["expected"].add(expected)
        for i, label, r, platinum in channels:
            if i >= len(parts):
                continue
            v = _value(parts[i])
            if v is None:
                drop(label, "invalid")
            elif r is not None and r < len(parts) and _uncalibrated(parts[i], parts[r]):
                drop(label, "uncalibrated")
            elif platinum and v < PLATINUM_MIN_MK:
                drop(label, "below the sensor's range")
            else:
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


def _window_files(root: Path, start: datetime, end: datetime, pattern: str) -> list[Path]:
    """The log files that can hold rows in [start, end]."""
    start, end = lab_time(start), lab_time(end)
    out = []
    for f in sorted(root.glob(pattern)):
        began = _session_start(f.name)
        if began and lab_time(began) > end:
            continue
        if modified_at(f) < start:
            continue  # append-only: nothing written after the window began
        out.append(f)
    return out


def collect_readings(root: Path, start: datetime, end: datetime, pattern: str = LCR_GLOB,
                     dropped: dict | None = None, clock: dict | None = None,
                     ) -> tuple[dict[str, list[tuple[datetime, float]]], list[str], list[str]]:
    """Every labelled thermometer's (time, mK) readings in [start, end], time-sorted.

    Opens only files that can hold rows in the window, takes each channel's readings from
    its own ``_N`` file only, and skips header-less stubs. A thermometer the bridge did not
    scan in the window is present with an empty list. Returns (readings, files read, stubs
    skipped); ``dropped`` and ``clock`` collect what read_lcr_file left out and its
    clock check.
    """
    merged: dict[str, list[tuple[datetime, float]]] = {}
    files_read, stubs_skipped = [], []
    for f in _window_files(root, start, end, pattern):
        chunk = read_lcr_file(f, start, end, file_channel(f.name), dropped, clock)
        if chunk is None:
            stubs_skipped.append(f.name)
            continue
        files_read.append(f.name)
        for label, pts in chunk.items():
            merged.setdefault(label, []).extend(pts)
    for pts in merged.values():
        pts.sort()
    return merged, files_read, stubs_skipped


# Below this, a mixing-chamber reading means the fridge is at base.
BASE_MK = 300.0
_BACKUP_WINDOW = timedelta(minutes=10)


def is_cmn(label: str) -> bool:
    """A CMN (paramagnetic salt) thermometer, by its label."""
    return "CMN" in label.upper()


def backed_cmn_base(readings: dict[str, list[tuple[datetime, float]]], label: str,
                    ) -> tuple[list[tuple[datetime, float]], int]:
    """A CMN channel's sub-base readings that another thermometer agrees with, and how many
    it does not.

    A CMN salt is only meaningful when cold. Warm, it still reads about 10 mK, and on
    2026-07-27 a report printed "MXC base temperature, CMN223: min 9.4 mK" while the RuOx
    read 0.4-3.3 K. So a CMN reading counts as base only if some resistive thermometer also
    read below BASE_MK within ten minutes of it.
    """
    refs = [[(t, v) for t, v in pts] for name, pts in readings.items()
            if pts and not is_cmn(name)]
    times = [[t for t, _ in pts] for pts in refs]
    backed, unbacked = [], 0
    for ts, v in readings.get(label, []):
        if v >= BASE_MK:
            continue
        ok = False
        for pts, tl in zip(refs, times):
            i = bisect.bisect_left(tl, ts - _BACKUP_WINDOW)
            while i < len(tl) and tl[i] <= ts + _BACKUP_WINDOW:
                if pts[i][1] < BASE_MK:
                    ok = True
                    break
                i += 1
            if ok:
                break
        if ok:
            backed.append((ts, v))
        else:
            unbacked += 1
    return backed, unbacked


def _header(f: Path) -> tuple[list[str], list[str]] | None:
    """A channel file's column names and labels, or None for a header-less stub."""
    with f.open("r", encoding="utf-8", errors="replace") as fh:
        head = [fh.readline() for _ in range(4)]
    if not head[3].startswith("Date(String)"):
        return None
    return ([x.strip() for x in head[2].rstrip("\r\n").split("\t")],
            [x.strip() for x in head[3].rstrip("\r\n").split("\t")])


def channel_map(root: Path, start: datetime, end: datetime, pattern: str = LCR_GLOB
                ) -> dict[int, dict]:
    """{channel: {"labels": {...}, "measured": bool}} for the logs written in the window.

    Channels are numbered as the logger numbers them, from 0: file ``_N.dat``, column
    ``TN``. "measured" means the channel has its own file in the window. People count them
    differently: a lab member asked for "channel 4" meaning TT-26006, which is channel 3,
    and got the CMN223 on channel 4. So a CSV says which thermometer each number is.
    """
    out: dict[int, dict] = {}
    for f in _window_files(root, start, end, pattern):
        n = file_channel(f.name)
        hdr = _header(f) if n is not None else None
        if hdr is None:
            continue
        names, labels = hdr
        for i, name in enumerate(names):
            m = re.fullmatch(r"T(\d)", name)
            if m and i < len(labels) and _is_thermometer(labels[i]):
                ch = out.setdefault(int(m.group(1)), {"labels": set(), "measured": False})
                ch["labels"].add(labels[i])
        if n in out:
            out[n]["measured"] = True
    return out


def _resolve(wanted: str, files: list[tuple[Path, int, str]]) -> tuple[set, str | list[str]]:
    """The (channel, label) pairs ``wanted`` names, and how it named them: "number",
    "label" or "partial"; or, for a partial name that fits several labels, those labels."""
    number = re.fullmatch(r"[Tt]?(\d)", wanted.strip())
    if number:
        n = int(number.group(1))
        return {(c, lab) for _, c, lab in files if c == n}, "number"
    w = wanted.strip().lower()
    exact = {(c, lab) for _, c, lab in files if lab.lower() == w}
    if exact:
        return exact, "label"
    partial = {(c, lab) for _, c, lab in files if w and w in lab.lower()}
    labels = sorted({lab for _, lab in partial})
    if len(labels) > 1:
        return set(), labels
    return partial, "partial"


def _empty_series(pattern: str = LCR_GLOB) -> dict:
    return {"readings": [], "labels": set(), "channels": set(), "files": [], "dropped": {},
            "pattern": pattern, "matched_by": None, "ambiguous": [], "map": {},
            "clock": new_clock()}


def channel_series(root: Path, start: datetime, end: datetime, wanted: str) -> dict:
    """One thermometer's readings for a CSV, from its own log file only.

    ``wanted`` is a label ("RuOx12980"), part of one if only one label contains it ("TT"),
    or a channel ("5", "T5"). Labels move between channels from one session to the next,
    so a label is matched in each file's header. Agents used to write their own extraction
    instead, and each one merged every channel file: a channel-4 CSV sent to a lab member
    had 3,747 rows for about 1,250 measurements.

    Returns {"readings": [(ts, mK)], "labels": {...}, "channels": {...}, "files": [...],
    "dropped": {reason: n}, "pattern": glob, "matched_by": "number" | "label" | "partial",
    "ambiguous": [labels a partial name fits], "map": channel_map(...), "clock": the clock
    check (``clock_note``)}. Times are aware; see ``lab_time``.
    """
    for pattern in (LCR_GLOB, "LogLCR2___*.dat"):
        files = []
        for f in _window_files(root, start, end, pattern):
            n = file_channel(f.name)
            hdr = _header(f) if n is not None else None
            if hdr is None:
                continue
            names, labels = hdr
            col = names.index(f"T{n}") if f"T{n}" in names else None
            if col is not None and col < len(labels):
                files.append((f, n, labels[col]))
        if not files:
            continue
        out = _empty_series(pattern)
        out["map"] = channel_map(root, start, end, pattern)
        hits, how = _resolve(wanted, files)
        if isinstance(how, list):
            out["ambiguous"] = how
            return out
        out["matched_by"] = how
        for f, n, label in files:
            if (n, label) not in hits:
                continue
            dropped: dict = {}
            chunk = read_lcr_file(f, start, end, n, dropped, out["clock"])
            if chunk is None:
                continue
            out["readings"] += chunk.get(label, [])
            out["labels"].add(label)
            out["channels"].add(n)
            out["files"].append(f.name)
            for reason, k in dropped.get(label, {}).items():
                out["dropped"][reason] = out["dropped"].get(reason, 0) + k
        if out["files"]:
            out["readings"].sort()
            return out
    return _empty_series()


MC_TAG = "[MC"


def cmn_comparison(root: Path, start: datetime, end: datetime, label: str,
                   readings: list[tuple[datetime, float]]) -> dict | None:
    """How a CMN channel compares with a resistive mixing-chamber thermometer read at the
    same times while that one was off base; None if ``label`` is not a CMN.

    A CMN salt only reads correctly near base. On 2026-10-29 a lab member heated the MC to
    1000 mK and was sent the CMN223: TT-26006 [MC] and the RuOx read about 1000 mK while the
    CMN read 1e4-5e5 mK. Warm, a CMN can also read about 10 mK. Neither shows in the CMN's
    own column, so it is set beside a resistive one. Returns {"reference": label or None,
    "channel", "paired", "off_base", "ratio_min", "ratio_median", "ratio_max"}.
    """
    if not is_cmn(label):
        return None
    refs = sorted((n, lab) for n, ch in channel_map(root, start, end).items()
                  if ch["measured"] for lab in ch["labels"] if MC_TAG in lab and not is_cmn(lab))
    best: dict = {"reference": None, "paired": -1}
    for n, ref in refs:
        pts = channel_series(root, start, end, ref)["readings"]
        times = [t for t, _ in pts]
        paired, ratios = 0, []
        for ts, v in readings:
            i = bisect.bisect_left(times, ts)
            near = [j for j in (i - 1, i) if 0 <= j < len(pts)]
            if not near:
                continue
            j = min(near, key=lambda k: abs(times[k] - ts))
            if abs(times[j] - ts) > _BACKUP_WINDOW:
                continue
            paired += 1
            if pts[j][1] >= BASE_MK:
                ratios.append(v / pts[j][1])
        if paired > best["paired"]:
            ratios.sort()
            best = {"reference": ref, "channel": n, "paired": paired, "off_base": len(ratios),
                    "ratio_min": ratios[0] if ratios else None,
                    "ratio_median": median(ratios) if ratios else None,
                    "ratio_max": ratios[-1] if ratios else None}
    if best["reference"] is None:
        best["paired"] = 0
    return best


def recent_thermometry(data_path: str | Path, start: datetime, end: datetime) -> dict:
    """Summarise every labelled thermometer between ``start`` and ``end``.

    Returns plain data (no prose) so a caller can format it: per channel the latest
    reading and its time, and min / median / max / count over the window, all in mK;
    the thermometers the bridge did not scan; when the log was last written; which files
    were read or skipped; the newest of the other logs, so a missing pressure log is
    visible rather than silent; and ``clock_note`` when the log's clock was off. Times are
    ISO strings with their UTC offset ("2026-11-01 01:30:05-06:00").
    """
    root = Path(data_path)
    if not root.is_dir():
        return {"found": False, "reason": f"DR data folder not found: {root}"}

    start, end = lab_time(start), lab_time(end)
    clock = new_clock()
    merged, files_read, stubs_skipped = collect_readings(root, start, end, clock=clock)

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
        if is_cmn(label) and last_v < BASE_MK:
            backed, _ = backed_cmn_base(merged, label)
            if not backed or backed[-1][0] < last_ts - _BACKUP_WINDOW:
                channels[label]["doubtful"] = (
                    "reads below 300 mK but no other thermometer does: a CMN salt reads "
                    "about 10 mK when warm, so this does not show the fridge is at base")

    newest = max((datetime.fromisoformat(c["latest_at"]) for c in channels.values()),
                 default=None)
    other_logs = {}
    for pattern in OTHER_LOG_GLOBS:
        found = sorted(root.glob(pattern), key=modified_at)
        other_logs[pattern.split("___")[0]] = (
            modified_at(found[-1]).isoformat(sep=" ", timespec="minutes") if found else None)

    return {
        "found": bool(channels),
        "window_start": start.isoformat(sep=" ", timespec="minutes"),
        "window_end": end.isoformat(sep=" ", timespec="minutes"),
        "newest_reading_at": newest.isoformat(sep=" ") if newest else None,
        "channels": channels,
        "not_scanned": sorted(label for label, pts in merged.items() if not pts),
        "files_read": files_read,
        "stub_files_skipped": stubs_skipped,
        "other_logs_last_written": other_logs,
        "clock_problem": clock_note(clock),
    }
