"""LORE pipeline package.

Importing this package fails loudly if it is running under an interpreter that
lacks the project's dependencies, because the alternative is worse than a crash.

Observed live (2026-09-06): a Slack session resolved bare `python` to a separate
3.11 install with none of the project's packages, hit `ModuleNotFoundError`, spent
ten tool calls hunting for a `.venv` that does not exist, and finally ran
`pip install python-dotenv` into that interpreter to get moving. It "worked" —
and left the machine with two divergent Python environments, one of which is
still missing pydantic, markdown, lightrag and sentence-transformers. A report run
under it would fail immediately, and the same instinct would have installed a
much larger pile of packages to get past that.

So a cryptic ModuleNotFoundError is replaced with an instruction naming the exact
interpreter to use. Telling the caller the answer is the only reliable way to stop
it inventing one.
"""
from __future__ import annotations

_SENTINEL_DEP = "pydantic"   # required by lab_agent.models, i.e. by everything


def _configured_python() -> str:
    """The interpreter recorded in lab_config.md, or '' if unavailable."""
    try:
        import re
        from pathlib import Path

        cfg = Path(__file__).resolve().parents[1] / "lab_config.md"
        if not cfg.exists():
            return ""
        m = re.search(r"^\|\s*PYTHON\s*\|\s*([^|]+?)\s*\|", cfg.read_text(encoding="utf-8"),
                      re.MULTILINE)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def _check_interpreter() -> None:
    import importlib.util

    if importlib.util.find_spec(_SENTINEL_DEP) is not None:
        return

    import sys

    configured = _configured_python()
    lines = [
        "",
        "LORE is running under the wrong Python interpreter.",
        f"  this interpreter : {sys.executable}",
        f"  missing          : {_SENTINEL_DEP} (and probably markdown, lightrag, sentence-transformers)",
    ]
    if configured:
        lines += [
            f"  use instead      : {configured}",
            "",
            f'Re-run the same command with "{configured}" in place of `python`.',
        ]
    else:
        lines += [
            "",
            "Add a PYTHON row to lab_config.md pointing at the interpreter that has the",
            "project's dependencies, then re-run with it.",
        ]
    lines += [
        "",
        "Do NOT `pip install` to get past this. The packages are not missing — they are",
        "installed in the other interpreter, and installing them here creates a second,",
        "divergent environment that will drift out of sync.",
        "",
    ]
    raise RuntimeError("\n".join(lines))


_check_interpreter()
del _check_interpreter
