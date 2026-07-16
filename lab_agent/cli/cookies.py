"""
Log into LabArchives via WashU SSO (headed Playwright browser) and update
LA_SESSION_COOKIE in .env automatically (invoked via the root-level
get_la_cookies.py shim).

Usage:
    python get_la_cookies.py

The browser window will open visibly. When it reaches the Duo MFA step,
approve the push notification on your phone. The script will wait up to
3 minutes for you to complete MFA.
"""
from __future__ import annotations

import sys

from dotenv import dotenv_values

from ..config import ENV_PATH
from ..sources.labarchives.auth import cookies_still_valid


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    env = dotenv_values(ENV_PATH)
    email = env.get("LA_EMAIL_WU", "")
    password = env.get("LA_PASSWORD_WU", "")

    if not email or not password:
        print("ERROR: LA_EMAIL_WU and LA_PASSWORD_WU must be set in .env")
        sys.exit(1)

    existing_cookie = env.get("LA_SESSION_COOKIE", "")
    if cookies_still_valid(existing_cookie):
        print("LA_SESSION_COOKIE is still valid — no login needed.")
        sys.exit(0)

    print("Existing cookies expired or missing. Opening browser for WashU SSO login...")
    print("If Duo MFA is required, approve the push notification on your phone.")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx  = browser.new_context()
        page = ctx.new_page()

        # Step 1: go to LabArchives login
        page.goto("https://auth-service.labarchives.com/", wait_until="networkidle")

        # Step 2: select WashU institution
        try:
            page.select_option("select", label="Washington University in St. Louis")
            page.click("text=Go to Institution's Login")
            page.wait_for_load_state("networkidle")
        except Exception as e:
            print(f"Note: institution selector step skipped ({e})")

        # Step 3: fill WashU login form
        try:
            page.wait_for_selector("#ucWUSTLKeyLogin_txtUsername", timeout=10_000)
            page.fill("#ucWUSTLKeyLogin_txtUsername", email)
            page.fill("#ucWUSTLKeyLogin_txtPassword", password)
            page.click("#ucWUSTLKeyLogin_btnLogin")
            print("Credentials submitted. Waiting for MFA / redirect (up to 3 min)...")
        except PWTimeout:
            print("Could not find WashU login form. Complete login manually in the browser, then press Enter.")
            input()

        # Step 4: wait for successful login
        try:
            page.wait_for_url("**/mynotebook.labarchives.com/**", timeout=180_000)
        except PWTimeout:
            print(f"Did not reach mynotebook.labarchives.com. Current URL: {page.url}")
            print("Complete login manually, then press Enter.")
            input()

        print(f"Logged in! Current URL: {page.url}")

        # Step 5: collect LabArchives cookies
        all_cookies = ctx.cookies()
        la_cookies  = [c for c in all_cookies if "labarchives.com" in c["domain"]]

        if not la_cookies:
            print("WARNING: No labarchives.com cookies found after login.")
            browser.close()
            sys.exit(1)

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
