"""The hook that keeps LORE's Slack sessions off the raw fridge logs (lab_agent.slack.session_guard).

On 2026-11-06 a session parsed the LogLCR files itself, merged every channel's T4 column and
invented an "in calibration" range; a lab member got a CMN223 file reading 5e5 mK. The rule
in CLAUDE.md is now enforced: a tool call naming a LogLCR file or the DR data folder is
refused, and run_dr.py, which reads the folder from .env itself, still works.

Run: python tests/run_all.py session_guard
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lab_agent.slack import session_guard as G

DATA = "C:/LC Data/Data"


def test_reading_a_fridge_log_is_refused_however_it_is_named():
    for tool_input in (
        {"command": 'head -3 "C:/LC Data/Data/LogLCR___2026-10-28-08-45-29_4.dat"'},
        {"command": 'ls "C:/LC Data/Data" | head'},
        {"command": 'ls "/c/LC Data/Data"'},
        {"file_path": "C:\\LC Data\\Data\\LogLCR___2026-10-28-08-45-29_4.dat"},
        {"pattern": "*.dat", "path": "C:\\LC Data\\Data"},
        {"command": "python -c \"open(r'C:\\LC Data\\Data\\x.dat')\""},
    ):
        assert G.blocked(tool_input, DATA), tool_input


def test_the_sanctioned_ways_still_work():
    for tool_input in (
        {"command": 'python run_dr.py "2026-10-29 13:00" "2026-10-29 19:00" --csv TT'},
        {"command": "python run_dr.py --last 2"},
        {"file_path": "outputs/dr_csv/ch3_TT-26006-MC_20261029-1300_20261029-1900.csv"},
        {"command": "python -m lab_agent.cli.slack upload --file outputs/dr_csv/x.csv"},
    ):
        assert not G.blocked(tool_input, DATA), tool_input


def test_the_hook_exits_2_with_the_way_to_do_it():
    guard = Path(G.__file__)
    event = {"tool_name": "Bash", "tool_input": {"command": "head LogLCR___x_4.dat"}}
    run = subprocess.run([sys.executable, str(guard)], input=json.dumps(event),
                         capture_output=True, text=True, env={"DR_DATA_PATH": DATA})
    assert run.returncode == 2 and "run_dr.py" in run.stderr, (run.returncode, run.stderr)
    ok = subprocess.run([sys.executable, str(guard)], input="not json", capture_output=True,
                        text=True)
    assert ok.returncode == 0, "a hook that cannot read its input must not block"


def test_every_slack_session_is_started_with_the_guard():
    from lab_agent.slack import sessions

    cfg = json.loads(sessions.session_settings().read_text(encoding="utf-8"))
    (entry,) = cfg["hooks"]["PreToolUse"]
    assert "Bash" in entry["matcher"] and "Read" in entry["matcher"], entry
    assert "session_guard.py" in entry["hooks"][0]["command"], entry
    src = Path(sessions.__file__).read_text(encoding="utf-8")
    assert '--settings "{settings}"' in src, "the spawn command no longer passes the guard"


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
