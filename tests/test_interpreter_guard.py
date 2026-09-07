"""The wrong interpreter must fail with an instruction, not a ModuleNotFoundError.

Observed live: a Slack session resolved bare `python` to a 3.11 install with none
of the project's packages, spent ten tool calls hunting for a non-existent .venv,
and finally ran `pip install python-dotenv` into it. Documentation alone did not
stop that — the very next session still used bare `python`. So the package itself
now says what to do, because an agent handed a cryptic error will invent a fix.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASSED = 0


def check(label: str, cond: bool) -> None:
    global PASSED
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    PASSED += 1
    print(f"  ok  {label}")


def test_configured_python_is_read_from_lab_config() -> None:
    from lab_agent import _configured_python

    cfg = ROOT / "lab_config.md"
    if not cfg.exists():
        check("no lab_config.md on this machine — skipping the parse check", True)
        return

    want = re.search(r"^\|\s*PYTHON\s*\|\s*([^|]+?)\s*\|", cfg.read_text(encoding="utf-8"),
                     re.MULTILINE)
    if not want:
        check("lab_config.md has no PYTHON row — skipping", True)
        return
    check("the PYTHON row is parsed out of lab_config.md",
          _configured_python() == want.group(1).strip())


def test_right_interpreter_imports_cleanly() -> None:
    import lab_agent  # noqa: F401  — this test only runs under the right one
    check("the correct interpreter imports lab_agent without complaint", True)


def test_wrong_interpreter_gets_an_instruction() -> None:
    """Run the guard under an interpreter with no pydantic and read the message."""
    other = Path("C:/Users/axelr/AppData/Local/Programs/Python/Python311/python.exe")
    if not other.exists():
        check("no second interpreter on this machine — skipping the live check", True)
        return
    probe = subprocess.run(
        [str(other), "-c", "import importlib.util,sys; "
                           "sys.exit(0 if importlib.util.find_spec('pydantic') is None else 1)"],
        capture_output=True,
    )
    if probe.returncode != 0:
        check("the second interpreter now has pydantic too — skipping", True)
        return

    r = subprocess.run([str(other), "-c", "import lab_agent"],
                       cwd=str(ROOT), capture_output=True, text=True)
    err = r.stderr
    check("it fails rather than half-working", r.returncode != 0)
    check("it names the problem plainly", "wrong Python interpreter" in err)
    check("it names the interpreter actually in use", "Python311" in err)
    check("it names the one to use instead", "miniconda" in err or "PYTHON row" in err)
    check("it forbids pip install as a workaround", "Do NOT `pip install`" in err)
    check("it is not a bare ModuleNotFoundError",
          "ModuleNotFoundError: No module named 'pydantic'" not in err.splitlines()[-1:][0]
          if err.splitlines() else True)


if __name__ == "__main__":
    test_configured_python_is_read_from_lab_config()
    test_right_interpreter_imports_cleanly()
    test_wrong_interpreter_gets_an_instruction()
    print(f"\ntest_interpreter_guard: {PASSED} checks passed")
