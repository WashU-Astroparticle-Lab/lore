"""
Log into LabArchives via your institution's SSO (headed Playwright browser)
and update
LA_SESSION_COOKIE in .env automatically (invoked via the root-level
get_la_cookies.py shim).

Usage:
    python get_la_cookies.py

The browser window will open visibly. When it reaches the Duo MFA step,
approve the push notification on your phone. The script will wait up to
3 minutes for you to complete MFA.

Run with no terminal (from a Slack session), it never waits for Enter: whatever it
cannot finish on its own ends in a `COOKIE_REFRESH_FAILED: <reason>` line and exit 1,
so the session can tell the user why.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from dotenv import dotenv_values

from ..config import ENV_PATH, lab_config_value
from ..sources.labarchives.auth import cookies_still_valid

SSO_ROWS = ("SSO institution label", "SSO form prefix")


def sso_settings_problem(settings: dict[str, str], interactive: bool) -> str | None:
    """Why the SSO login cannot run, or None.

    Without both lab_config.md rows only a person can log in, so a missing row is fatal
    only when there is no terminal to hand the browser over to. A Slack session hit exactly
    that: the rows had never been added on the lab machine, the script searched the login
    dropdown for an empty name, waited for an Enter key nobody could press, and crashed --
    and the session asked the user to run the script, which would have failed the same way.
    """
    missing = [row for row in SSO_ROWS if not settings.get(row)]
    if not missing or interactive:
        return None
    rows = " and ".join(f"'{row}'" for row in missing)
    return (f"lab_config.md has no {rows} row, so the login cannot be automated. Add it "
            "(see lab_config.template.md), or run get_la_cookies.py in a terminal on this "
            "machine and log in by hand.")


def with_env_value(text: str, key: str, value: str) -> str:
    """``text`` (a .env file) with ``key`` set to ``value``, everything else untouched.

    Every active ``key=`` line is replaced, including ``export key=`` and indented ones;
    python-dotenv reads the last of several. The old rewrite replaced only a line starting
    exactly with ``key=``, while its substring check still printed "updated" when none did.
    Line endings are kept.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    lines = text.splitlines()
    hit = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}"
            hit = True
    if not hit:
        lines.append(f"{key}={value}")
    return newline.join(lines) + newline


def write_env_value(path, key: str, value: str) -> None:
    """Set one .env value, writing a temporary file and swapping it in.

    The file holds every credential, and the old code truncated it before rewriting: a
    crash or a full disk in between would have emptied it.
    """
    path = Path(path)
    # Bytes, not read_text: text mode turns CRLF into LF, and the endings must survive.
    text = path.read_bytes().decode("utf-8") if path.exists() else ""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(with_env_value(text, key, value))
    os.replace(tmp, path)


def _fail(reason: str) -> None:
    print(f"COOKIE_REFRESH_FAILED: {reason}", flush=True)
    sys.exit(1)


def _hand_over(instruction: str, reason: str, interactive: bool) -> None:
    """Let a person at the terminal finish by hand; with no terminal, stop with ``reason``."""
    if not interactive:
        _fail(reason)
    print(f"{instruction} Then press Enter.")
    input()


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        _fail("playwright not installed. Run: pip install playwright && playwright install chromium")

    env = dotenv_values(ENV_PATH)
    email = env.get("LA_EMAIL_WU", "")
    password = env.get("LA_PASSWORD_WU", "")

    if not email or not password:
        _fail("LA_EMAIL_WU and LA_PASSWORD_WU must be set in .env")

    existing_cookie = env.get("LA_SESSION_COOKIE", "")
    if cookies_still_valid(existing_cookie):
        print("LA_SESSION_COOKIE is still valid — no login needed.")
        sys.exit(0)

    interactive = sys.stdin.isatty()
    # The label and the login-form field ids are specific to one university's SSO, so
    # they live in lab_config.md rather than here -- another lab changes config, not code.
    settings = {row: lab_config_value(row) for row in SSO_ROWS}
    problem = sso_settings_problem(settings, interactive)
    if problem:
        _fail(problem)
    institution, form = settings["SSO institution label"], settings["SSO form prefix"]

    print("Existing cookies expired or missing. Opening browser for SSO login...")
    print("If Duo MFA is required, approve the push notification on your phone.")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx  = browser.new_context()
        page = ctx.new_page()

        # Step 1: go to LabArchives login
        page.goto("https://auth-service.labarchives.com/", wait_until="networkidle")

        # Step 2: select the institution
        if institution:
            try:
                page.select_option("select", label=institution)
                page.click("text=Go to Institution's Login")
                page.wait_for_load_state("networkidle")
            except Exception as e:
                print(f"Note: institution selector step skipped ({e})")

        # Step 3: fill the institution login form
        submitted = False
        if form:
            try:
                page.wait_for_selector(f"{form}txtUsername", timeout=10_000)
                page.fill(f"{form}txtUsername", email)
                page.fill(f"{form}txtPassword", password)
                page.click(f"{form}btnLogin")
                submitted = True
                print("Credentials submitted. Waiting for MFA / redirect (up to 3 min)...")
            except PWTimeout:
                pass
        if not submitted:
            _hand_over(
                "Could not fill the institution login form. Complete login manually in the browser.",
                "the SSO login form never appeared. The 'SSO institution label' or 'SSO form "
                "prefix' in lab_config.md may not match the login pages, or those pages changed.",
                interactive)

        # Step 4: wait for successful login
        try:
            page.wait_for_url("**/mynotebook.labarchives.com/**", timeout=180_000)
        except PWTimeout:
            print(f"Did not reach mynotebook.labarchives.com. Current URL: {page.url}")
            _hand_over(
                "Complete login manually.",
                "the login did not reach LabArchives within 3 minutes (if an MFA prompt "
                "appeared, nobody approved it).",
                interactive)

        print(f"Logged in! Current URL: {page.url}")

        # Step 5: collect LabArchives cookies
        all_cookies = ctx.cookies()
        la_cookies  = [c for c in all_cookies if "labarchives.com" in c["domain"]]

        if not la_cookies:
            browser.close()
            _fail("logged in, but no labarchives.com cookies were found afterwards.")

        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in la_cookies)

        # Step 6: auto-update .env
        write_env_value(ENV_PATH, "LA_SESSION_COOKIE", cookie_str)

        print()
        print("LA_SESSION_COOKIE updated in .env automatically.")
        browser.close()


if __name__ == "__main__":
    main()
