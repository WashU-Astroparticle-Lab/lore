"""Tests for lab_agent.dr.conditions, the DR summary behind dr-status, dr-report and the
report pipeline's DR section.

The logs are built here in the Leiden software's own layout with the 2026 labels, where
the previous fixed-column, kelvin-limit parser went wrong: it dropped every
mixing-chamber reading and reported TT-26006 [MC] at 47 mK as "4K (TT-2450): 47 K".

The software writes one file per bridge channel (``_N.dat``), and each row repeats every
other channel's last reading. ``write_channels`` writes the same rows to several channel
files, which is enough wherever a test does not care about the copies; the copy pattern
itself is built row by row in ``test_each_channel_is_read_only_from_its_own_file``.

Run: python tests/run_all.py dr_conditions   (or: python tests/test_dr_conditions.py)
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from lab_agent.dr import get_dr_conditions
from lab_agent.dr.live import collect_readings, file_channel

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


SESSION = "LogLCR___2026-09-24-00-00-00"
# T1 3K Plate, T2 Still Plate, T3 TT-26006 [MC], T4 CMN223 [MC P], T6 50mK Plate.
SCANNED = (1, 2, 3, 4, 6)


def write_channels(folder: Path, rows: list[str], channels=SCANNED) -> None:
    for n in channels:
        write(folder, f"{SESSION}_{n}.dat", rows)


def minutes(start: datetime, n: int, step: float = 1.0) -> list[datetime]:
    return [start + timedelta(minutes=i * step) for i in range(n)]


def test_mc_thermometers_are_read_by_label_in_mk():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write_channels(d, [row(t, tt=f"{45 + (i % 3)}.0E+0", plate3k=f"{2890 + i % 5}E+0")
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
        write_channels(d, rows)
        md = get_dr_conditions(d, DAY, window_hours=0)
    assert "## Thermal Event Warnings" in md, md
    assert "readings of TT-26006 [MC]" in md, "the gap must be watched on the resistive MC thermometer"
    assert "Peak MXC temperature:** 3.200 K" in md, md
    assert "significant" in md, md


def test_each_channel_is_read_only_from_its_own_file():
    # The real pattern: the bridge reads channel 3, then channel 4, ten seconds apart. Each
    # file's row repeats the other channel's last reading, stamped with its own time.
    # Merging every column counted each reading twice, ten seconds late the second time.
    t0 = DAY + timedelta(hours=10)
    tt = [f"{40 + i}.0E+0" for i in range(6)]
    cmn = [f"{12 + i / 10:.1f}E+0" for i in range(6)]
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write(d, f"{SESSION}_3.dat", [row(t0 + timedelta(seconds=20 * i), tt=tt[i],
                                          cmn=cmn[i - 1] if i else "NaN") for i in range(6)])
        write(d, f"{SESSION}_4.dat", [row(t0 + timedelta(seconds=20 * i + 10), tt=tt[i],
                                          cmn=cmn[i]) for i in range(6)])
        readings, files, _ = collect_readings(d, t0, t0 + timedelta(minutes=5))
        md = get_dr_conditions(d, DAY, window_hours=0)
    assert [ts.second for ts, _ in readings["TT-26006 [MC]"]] == [0, 20, 40, 0, 20, 40], (
        "TT-26006 readings must carry the times channel 3 was measured, not channel 4's copies")
    assert [v for _, v in readings["TT-26006 [MC]"]] == [40, 41, 42, 43, 44, 45]
    assert [ts.second for ts, _ in readings["CMN223 [MC P]"]] == [10, 30, 50, 10, 30, 50]
    assert "| TT-26006 [MC] | 40.0 mK | 42.5 mK | 45.0 mK | 45.0 mK (09-24 10:01) | 6 |" in md, md
    assert readings["3K Plate"] == [] and readings["Still Plate"] == [], (
        "a thermometer with no file of its own was not scanned; its copies are not readings")
    assert len(files) == 2


def test_a_channel_the_bridge_did_not_scan_is_not_given_a_value():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write(d, f"{SESSION}_3.dat", [row(t, tt=f"{45 + (i % 3)}.0E+0") for i, t in
                                      enumerate(minutes(DAY + timedelta(hours=1), 150))])
        md = get_dr_conditions(d, DAY, window_hours=0)
    # Only channel 3 (TT-26006) has a file; the rest of each row is copies.
    assert "**Not measured in this window:** 3K Plate, Still Plate, CMN223 [MC P], 50mK Plate." in md, md
    assert "| 3K Plate" not in md and "| CMN223" not in md, "a copy was reported as a reading"
    assert "MXC base temperature, CMN223" not in md, "an unscanned MC value was presented as base"
    assert "MXC base temperature, TT-26006 [MC]" in md, md


def test_a_file_without_a_channel_number_is_read_whole_and_held_values_flagged():
    # No such file exists on the lab PC today; if one appears, every column is read and
    # the held-value check is what keeps a frozen copy from passing as a temperature.
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write(d, f"{SESSION}.dat", [row(t, tt=f"{45 + (i % 3)}.0E+0") for i, t in
                                    enumerate(minutes(DAY + timedelta(hours=1), 150))])
        md = get_dr_conditions(d, DAY, window_hours=0)
    assert "| 3K Plate (held value) |" in md, md
    assert "**Held values:**" in md and "Do not use it" in md, md
    assert "MXC base temperature, CMN223" not in md, "a held MC value was presented as base"
    assert "MXC base temperature, TT-26006 [MC]" in md, md


FULL_NAMES = "Date1\tDate2\tR0\tR1\tR2\tR3\tR4\tT0\tT1\tT2\tT3\tT4"
FULL_LABELS = ("Date(String)\tDate(Number)\tC_still [Still]\t3K Plate\tMC Plate\t"
               "TT-26006 [MC]\tCMN223 [MC P]\tC_still [Still]\t3K Plate\tMC Plate\t"
               "TT-26006 [MC]\tCMN223 [MC P]")
CALIBRATIONS = "\t\tNo Calibration\tRuO2\tPT1000\tTT-26006\tCMN-223\tNo Calibration\tRuO2\tPT1000\tTT-26006\tCMN-223"


def full_row(ts, r3="12895.14", t3="76.0", t4="11.8", t2="70000", r2="1100") -> str:
    return "\t".join([ts.strftime("%Y-%m-%d %H:%M:%S"), "3.87E+9", "1.0", "2000", r2, r3, "5",
                      "1.9E-12", "2890", t2, t3, t4])


def write_full(folder: Path, n: int, rows: list[str]) -> None:
    (folder / f"{SESSION}_{n}.dat").write_text(
        "\n".join(["#", "", FULL_NAMES, FULL_LABELS, CALIBRATIONS] + rows) + "\n", encoding="utf-8")


def test_an_uncalibrated_channel_is_not_reported_as_a_temperature():
    # 2026-07-27: TT-26006 had no calibration, so the log held its resistance in the T column
    # and "2.2 kOhm" was reported as 2.2 K. From 15:13 it was calibrated: those rows count.
    t0 = DAY + timedelta(hours=13)
    rows = [full_row(t0 + timedelta(minutes=i), r3=f"{2220.5 + i}", t3=f"{2220.5 + i}")
            for i in range(10)]
    rows += [full_row(t0 + timedelta(minutes=10 + i), r3=f"{12895 + i}", t3=f"{76 - i}.0")
             for i in range(5)]
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write_full(d, 3, rows)
        md = get_dr_conditions(d, DAY, window_hours=0)
    assert "| TT-26006 [MC] | 72.0 mK | 74.0 mK | 76.0 mK |" in md, md
    assert "**Left out:** 10 uncalibrated reading(s) of TT-26006 [MC]" in md, md
    assert "2.220 K" not in md


def test_a_platinum_sensor_below_its_range_is_left_out():
    t0 = DAY + timedelta(hours=1)
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write_full(d, 2, [full_row(t0 + timedelta(minutes=i), t2=t2)
                          for i, t2 in enumerate(["60.79", "189.0", "45000", "52000"])])
        md = get_dr_conditions(d, DAY, window_hours=0)
    assert "| MC Plate | 45.0 K | 48.5 K | 52.0 K |" in md, md
    assert "2 below the sensor's range reading(s) of MC Plate" in md, md


def test_a_warm_cmn_is_not_called_base():
    # 2026-07-27: "MXC base temperature, CMN223: min 9.4 mK" while the RuOx read 0.4-3.3 K.
    t0 = DAY + timedelta(hours=13)
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write_full(d, 3, [full_row(t0 + timedelta(minutes=2 * i), t3="1500.0") for i in range(10)])
        write_full(d, 4, [full_row(t0 + timedelta(minutes=2 * i, seconds=30), t4="9.5")
                          for i in range(10)])
        warm = get_dr_conditions(d, DAY, window_hours=0)
        write_full(d, 3, [full_row(t0 + timedelta(minutes=2 * i), t3="38.0") for i in range(10)])
        cold = get_dr_conditions(d, DAY, window_hours=0)
    assert "MXC base temperature, CMN223" not in warm, warm
    assert "**CMN223 [MC P]:** 10 reading(s) below 300 mK" in warm, warm
    assert "MXC base temperature, CMN223 [MC P] (sub-300 mK readings only):** min 9.5 mK" in cold, cold


def test_one_thermometer_exports_from_its_own_file():
    from lab_agent.dr.live import channel_series

    t0 = DAY + timedelta(hours=10)
    tt = [f"{40 + i}.0E+0" for i in range(4)]
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write(d, f"{SESSION}_3.dat", [row(t0 + timedelta(seconds=20 * i), tt=tt[i]) for i in range(4)])
        write(d, f"{SESSION}_4.dat", [row(t0 + timedelta(seconds=20 * i + 10), tt=tt[i]) for i in range(4)])
        by_label = channel_series(d, t0, t0 + timedelta(minutes=5), "tt-26006 [mc]")
        by_number = channel_series(d, t0, t0 + timedelta(minutes=5), "T3")
        nothing = channel_series(d, t0, t0 + timedelta(minutes=5), "no such sensor")
    assert [ts.second for ts, _ in by_label["readings"]] == [0, 20, 40, 0], by_label
    assert by_label["files"] == [f"{SESSION}_3.dat"] and by_label["channels"] == {3}
    assert by_number["readings"] == by_label["readings"]
    assert nothing["files"] == [] and nothing["readings"] == []


def test_run_dr_windows_mean_what_they_say():
    from lab_agent.cli import run_dr

    now = datetime(2026, 11, 6, 17, 0)
    last = run_dr.parse_args(["--last", "2"], now=now)
    assert (last["mode"], last["start"], last["end"]) == ("last", datetime(2026, 11, 6, 15), now)
    days = run_dr.parse_args(["2025-02-17", "2025-02-19"])
    assert days["end"] == datetime(2025, 2, 19, 23, 59, 59), "the end day must be included"
    timed = run_dr.parse_args(["2025-02-18 14:00", "--hours", "6"])
    assert (timed["start"], timed["end"]) == (datetime(2025, 2, 18, 14), datetime(2025, 2, 18, 20))
    day = run_dr.parse_args(["2025-02-18"])
    assert day["mode"] == "day" and day["hours"] == 12
    csv = run_dr.parse_args(["2026-10-29 13:00", "2026-10-29 19:00", "--csv", "RuOx12980"])
    assert csv["csv"] == "RuOx12980" and csv["mode"] == "window"
    assert run_dr._folder_for(day).name == "dr_20250218"
    assert run_dr._folder_for(timed).name == "dr_20250218-1400_20250218-2000"


def test_the_channel_comes_from_the_file_name():
    assert file_channel("LogLCR___2026-10-28-08-45-29_5.dat") == 5
    assert file_channel("LogLCR2___2025-01-27-17-44-48_0.dat") == 0
    assert file_channel("LogLCR___2026-10-28-08-45-29.dat") is None, "a session stub has no channel"


def test_stubs_old_files_and_missing_pressure_are_handled_honestly():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write(d, f"{SESSION}_3.dat", [row(t) for t in minutes(DAY + timedelta(hours=10), 5)])
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
        write(d, f"{SESSION}_3.dat", [row(t) for t in minutes(DAY + timedelta(hours=10), 5)])
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
