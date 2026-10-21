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

import sys

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
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            env_text = f.read()

        new_line = f"LA_SESSION_COOKIE={cookie_str}"
        if "LA_SESSION_COOKIE=" in env_text:
            lines = env_text.splitlines()
            for j, line in enumerate(lines):
                if line.startswith("LA_SESSION_COOKIE="):
                    lines[j] = new_line
                    break
            env_text = "\n".join(lines) + "\n"
        else:
            env_text += f"\n{new_line}\n"

        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write(env_text)

        print()
        print("LA_SESSION_COOKIE updated in .env automatically.")
        browser.close()


if __name__ == "__main__":
    main()
