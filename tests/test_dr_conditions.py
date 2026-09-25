"""Tests for lab_agent.dr.conditions, the DR summary behind dr-status, dr-report and the
report pipeline's DR section.

The logs are built here in the Leiden software's own layout with the 2026 labels, where
the previous fixed-column, kelvin-limit parser went wrong: it dropped every
mixing-chamber reading and reported TT-26006 [MC] at 47 mK as "4K (TT-2450): 47 K".

Run: python tests/run_all.py dr_conditions   (or: python tests/test_dr_conditions.py)
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from lab_agent.dr import get_dr_conditions

NAMES = "Date1\tDate2\tR0\tT0\tT1\tT2\tT3\tT4\tT5\tT6"
LABELS = ("Date(String)\tDate(Number)\tC_still [Still]\tC_still [Still]\t3K Plate\t"
          "Still Plate\tTT-26006 [MC]\tCMN223 [MC P]\tOPEN\t50mK Plate")
DAY = datetime(2026, 9, 24)


def row(ts: datetime, tt="46.8E+0", cmn="12.48E+0", plate3k="2.89E+3", still="792.2E+0",
        plate50="66.7E+0") -> str:
    return "\t".join([ts.strftime("%Y-%m-%d %H:%M:%S"), "3.87E+9", "1.0", "1.9E-12",
                      plate3k, still, tt, cmn, "0.0E+0", plate50])


def write(folder: Path, name: str, rows: list[str], header: bool = True) -> None:
    head = ["#", "", NAMES, LABELS] if header else []
    (folder / name).write_text("\n".join(head + rows) + "\n", encoding="utf-8")


def minutes(start: datetime, n: int, step: float = 1.0) -> list[datetime]:
    return [start + timedelta(minutes=i * step) for i in range(n)]


def test_mc_thermometers_are_read_by_label_in_mk():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write(d, "LogLCR___2026-09-24-00-00-00_0.dat",
              [row(t, tt=f"{45 + (i % 3)}.0E+0", plate3k=f"{2890 + i % 5}E+0")
               for i, t in enumerate(minutes(DAY + timedelta(hours=10), 30))])
        md = get_dr_conditions(d, DAY, window_hours=0)
    assert md, "no summary for a window full of readings"
    assert "| TT-26006 [MC] | 45.0 mK | 46.0 mK | 47.0 mK |" in md, md
    assert "CMN223 [MC P] | 12.5 mK" in md, md
    assert "| 3K Plate | 2.890 K" in md, "values above 2 K are shown in K"
    assert "4K (TT-2450)" not in md and "46.8 K" not in md, "the old mislabelling is back"
    assert "MXC base temperature, CMN223 [MC P]" in md, md
    assert "C_still" not in md and "OPEN" not in md, "non-thermometer columns leaked in"


def test_a_warm_excursion_is_reported_as_a_thermal_event():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        t0 = DAY + timedelta(hours=10)
        rows = [row(t) for t in minutes(t0, 10)]
        # 20 minutes above 1 K, peaking at 3.2 K; the CMN salt drops out, as it does.
        rows += [row(t, tt=f"{1500 + 100 * i}E+0", cmn="NaN")
                 for i, t in enumerate(minutes(t0 + timedelta(minutes=10), 18))]
        rows += [row(t) for t in minutes(t0 + timedelta(minutes=30), 10)]
        write(d, "LogLCR___2026-09-24-00-00-00_0.dat", rows)
        md = get_dr_conditions(d, DAY, window_hours=0)
    assert "## Thermal Event Warnings" in md, md
    assert "readings of TT-26006 [MC]" in md, "the gap must be watched on the resistive MC thermometer"
    assert "Peak MXC temperature:** 3.200 K" in md, md
    assert "significant" in md, md


def test_a_held_value_is_not_reported_as_a_temperature():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write(d, "LogLCR___2026-09-24-00-00-00_0.dat",
              [row(t, tt=f"{45 + (i % 3)}.0E+0") for i, t in
               enumerate(minutes(DAY + timedelta(hours=1), 150))])
        md = get_dr_conditions(d, DAY, window_hours=0)
    # 3K Plate, Still Plate, 50mK Plate and the CMN are constant in this fixture.
    assert "| 3K Plate (held value) |" in md, md
    assert "**Held values:**" in md and "Do not use it" in md, md
    assert "MXC base temperature, CMN223" not in md, "a held MC value was presented as base"
    assert "MXC base temperature, TT-26006 [MC]" in md, md


def test_stubs_old_files_and_missing_pressure_are_handled_honestly():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write(d, "LogLCR___2026-09-24-00-00-00_0.dat",
              [row(t) for t in minutes(DAY + timedelta(hours=10), 5)])
        write(d, "LogLCR___2026-09-24-00-00-00.dat",
              [row(DAY + timedelta(hours=10), tt="9999E+0")], header=False)
        old = d / "LogLCR___2026-07-04-17-42-42_3.dat"
        write(d, old.name, [row(DAY + timedelta(hours=10, minutes=2), tt="7777E+0")])
        stamp = datetime(2026, 7, 10).timestamp()
        os.utime(old, (stamp, stamp))
        md = get_dr_conditions(d, DAY, window_hours=0)
        assert "9.999 K" not in md and "7.777 K" not in md, "a stub or stale file was read"
        assert "No pressure log (logFP) was written during this window" in md, md
        assert get_dr_conditions(d, datetime(2020, 1, 1), window_hours=0) is None
    assert get_dr_conditions(Path(tmp) / "missing", DAY) is None


def test_pressures_still_come_from_logfp_when_it_is_written():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write(d, "LogLCR___2026-09-24-00-00-00_0.dat",
              [row(t) for t in minutes(DAY + timedelta(hours=10), 5)])
        fp = ["#Date\tP1\tP2\tP3\tP4"] + [
            f"{t.strftime('%Y-%m-%d %H:%M:%S')}\t7.3\t-4.8\t172.1\t-4.8"
            for t in minutes(DAY + timedelta(hours=10), 5)]
        (d / "logFP___2026-09-24-00-00-00.dat").write_text("\n".join(fp) + "\n", encoding="utf-8")
        md = get_dr_conditions(d, DAY, window_hours=0)
    assert "| P1 (fore-line) | 7.3 | 7.3 | 7.3 | 5 |" in md, md
    assert "**Pressure samples:** 5" in md, md


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
