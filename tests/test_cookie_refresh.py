"""The cookie refresh must say why it failed, not wait for an Enter key nobody can press.

On 2026-10-20 a report session ran get_la_cookies.py itself, as the skill says. The lab
machine's lab_config.md had no SSO rows, so the script searched the login dropdown for an
empty name, fell through to `input()` with no terminal attached, and crashed. The session
then asked the user to run the script by hand, which would have failed the same way.

Run: python tests/run_all.py cookie_refresh   (or: python tests/test_cookie_refresh.py)
"""
from __future__ import annotations

import builtins
import contextlib
import io
import sys

from lab_agent.cli import cookies

BOTH = {"SSO institution label": "Example University", "SSO form prefix": "#exampleLogin_"}


def test_missing_rows_stop_a_session_with_the_reason():
    problem = cookies.sso_settings_problem({}, interactive=False)
    assert problem and "lab_config.md" in problem, problem
    assert "'SSO institution label'" in problem and "'SSO form prefix'" in problem, problem


def test_only_the_missing_row_is_named():
    problem = cookies.sso_settings_problem(
        {"SSO institution label": "Example University", "SSO form prefix": ""},
        interactive=False)
    assert problem and "'SSO form prefix'" in problem, problem
    assert "institution label" not in problem, problem


def test_complete_rows_pass():
    assert cookies.sso_settings_problem(BOTH, interactive=False) is None


def test_a_person_at_a_terminal_can_still_log_in_by_hand():
    assert cookies.sso_settings_problem({}, interactive=True) is None


def test_no_terminal_never_waits_for_enter():
    def no_input(*a):
        raise AssertionError("input() called with no terminal attached")

    out = io.StringIO()
    saved = builtins.input
    builtins.input = no_input
    try:
        with contextlib.redirect_stdout(out):
            cookies._hand_over("Log in by hand.", "the form never appeared.", interactive=False)
    except SystemExit as exc:
        assert exc.code == 1, exc.code
    else:
        raise AssertionError("_hand_over returned instead of stopping")
    finally:
        builtins.input = saved
    assert out.getvalue().strip() == "COOKIE_REFRESH_FAILED: the form never appeared.", out.getvalue()


def test_the_cookie_is_written_without_disturbing_the_rest_of_env():
    import tempfile
    from pathlib import Path

    crlf = "A=1\r\nLA_SESSION_COOKIE=old\r\n# LA_SESSION_COOKIE=commented\r\nB=2\r\n"
    out = cookies.with_env_value(crlf, "LA_SESSION_COOKIE", "new")
    assert out == "A=1\r\nLA_SESSION_COOKIE=new\r\n# LA_SESSION_COOKIE=commented\r\nB=2\r\n", out
    # Forms the old rewrite matched by substring but never replaced, printing "updated".
    for form in ("export LA_SESSION_COOKIE=old", "  LA_SESSION_COOKIE = old"):
        out = cookies.with_env_value(f"A=1\n{form}\n", "LA_SESSION_COOKIE", "new")
        assert out == "A=1\nLA_SESSION_COOKIE=new\n", (form, out)
    dup = cookies.with_env_value("LA_SESSION_COOKIE=a\nLA_SESSION_COOKIE=b\n", "LA_SESSION_COOKIE", "c")
    assert dup == "LA_SESSION_COOKIE=c\nLA_SESSION_COOKIE=c\n", "dotenv reads the last one"
    assert cookies.with_env_value("A=1\n", "LA_SESSION_COOKIE", "x") == "A=1\nLA_SESSION_COOKIE=x\n"
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env"
        env.write_bytes(crlf.encode("utf-8"))
        cookies.write_env_value(env, "LA_SESSION_COOKIE", "fresh")
        assert env.read_bytes() == crlf.replace("=old", "=fresh").encode("utf-8"), env.read_bytes()
        assert [p.name for p in Path(tmp).iterdir()] == [".env"], "a temp file was left behind"


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
