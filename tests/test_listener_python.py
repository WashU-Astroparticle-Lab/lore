"""Tests for the listener's interpreter check (lab_agent.config.interpreter_problem).

Twice the listener was restarted with a bare `python`, which on the lab PC is the
Microsoft Store install: it has slack_bolt, so the bot came up and answered, but not
LightRAG, so the knowledge-graph service never started and every question ran cold.
The listener now compares its environment with lab_config.md's PYTHON row and refuses
to start on any other.

Run: python tests/run_all.py listener_python   (or: python tests/test_listener_python.py)
"""
from __future__ import annotations

import sys

from lab_agent.config import interpreter_problem

VENV_PYTHON = "C:/lore_env/Scripts/python.exe"
STORE_PREFIX = (r"C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.13_3.13.3824.0"
                r"_x64__qbz5n2kfra8p0")
STORE_EXE = (r"C:\Users\someone\AppData\Local\Microsoft\WindowsApps"
             r"\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe")


def test_the_configured_venv_passes():
    assert interpreter_problem(VENV_PYTHON, r"C:\lore_env", r"C:\lore_env\Scripts\python.exe") is None
    # Same environment, other spelling or another of its executables: still fine.
    assert interpreter_problem(VENV_PYTHON, r"c:\LORE_ENV", r"C:\lore_env\Scripts\pythonw.exe") is None


def test_the_store_python_is_refused_with_the_command_to_use():
    problem = interpreter_problem(VENV_PYTHON, STORE_PREFIX, STORE_EXE)
    assert problem, "the Store Python was accepted"
    assert STORE_EXE in problem, "the message must say which interpreter this is"
    assert f'"{VENV_PYTHON}" slack_listener.py' in problem, "the message must give the command"


def test_a_conda_env_or_a_posix_venv_is_recognised():
    conda = r"C:\Users\someone\miniconda3\envs\presto\python.exe"
    assert interpreter_problem(conda, r"C:\Users\someone\miniconda3\envs\presto", conda) is None
    assert interpreter_problem(conda, r"C:\Users\someone\miniconda3", conda) is not None, (
        "conda's base environment is not the configured env")
    if sys.platform != "win32":
        assert interpreter_problem("/opt/lore/bin/python", "/opt/lore", "/opt/lore/bin/python") is None


def test_no_check_when_python_is_not_configured():
    assert interpreter_problem("", STORE_PREFIX, STORE_EXE) is None
    placeholder = ("<absolute path to the interpreter that has the project's dependencies, "
                   "e.g. C:/Users/you/miniconda3/python.exe>")
    assert interpreter_problem(placeholder, STORE_PREFIX, STORE_EXE) is None, (
        "another lab's untouched template must not lock them out")


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
