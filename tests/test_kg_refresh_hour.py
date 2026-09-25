"""The nightly KG refresh must run at KB_REFRESH_HOUR from .env.

It used to be read into a module constant at import, before the listener had loaded
.env, so the setting was ignored and the refresh ran at 2 a.m. On 2026-09-25 that put
a 22-minute graph rebuild in the middle of an overnight measurement whose .env said 12.

Run: python tests/run_all.py kg_refresh_hour   (or: python tests/test_kg_refresh_hour.py)
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path

import lab_agent.config as config
# Imported before any .env exists, exactly as the listener imports it.
from lab_agent.slack import sessions


@contextlib.contextmanager
def _env_file(text: str | None):
    """Point load_env() at a throwaway .env, with KB_REFRESH_HOUR unset in the process."""
    saved_path, saved_val = config.ENV_PATH, os.environ.pop("KB_REFRESH_HOUR", None)
    saved_loaded = config._env_loaded
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        if text is not None:
            path.write_text(text, encoding="utf-8")
        config.ENV_PATH = path
        config._env_loaded = False   # load_env() loads once per process; start fresh
        try:
            yield
        finally:
            config.ENV_PATH, config._env_loaded = saved_path, saved_loaded
            os.environ.pop("KB_REFRESH_HOUR", None)
            if saved_val is not None:
                os.environ["KB_REFRESH_HOUR"] = saved_val


def test_the_hour_in_env_file_is_used():
    with _env_file("KB_REFRESH_HOUR=12\n"):
        assert sessions.kg_refresh_hour() == 12


def test_no_setting_means_the_documented_default():
    with _env_file(""):
        assert sessions.kg_refresh_hour() == sessions.DEFAULT_KG_REFRESH_HOUR == 2
    with _env_file(None):
        assert sessions.kg_refresh_hour() == 2, "a missing .env must not be an error"


def test_a_value_that_is_not_an_hour_falls_back_rather_than_never_running():
    for bad in ("25", "-1", "noon", "12.5"):
        with _env_file(f"KB_REFRESH_HOUR={bad}\n"):
            assert sessions.kg_refresh_hour() == 2, bad


def test_nothing_reads_the_hour_at_import_any_more():
    assert not hasattr(sessions, "KG_REFRESH_HOUR"), (
        "a module-level hour is evaluated before .env is loaded; read it via kg_refresh_hour()")


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
