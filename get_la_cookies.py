"""
Helper script: log into LabArchives via WashU SSO (headed Playwright browser)
and print the session cookies to paste into .env as LA_SESSION_COOKIE.

Usage:
    python get_la_cookies.py

The browser window will open visibly. When it reaches the Duo MFA step,
approve the push notification on your phone. The script will wait up to
3 minutes for you to complete MFA.

After a successful login, it prints a line like:
    LA_SESSION_COOKIE=session_key=abc123; other=xyz

Paste that into your .env file.
"""
import os
import sys

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

from dotenv import dotenv_values

env = dotenv_values("C:/Users/axelr/OneDrive/Desktop/lab-agent-single-report/.env")
EMAIL = env.get("LA_EMAIL_WU", "")
PASSWORD = env.get("LA_PASSWORD_WU", "")

if not EMAIL or not PASSWORD:
    print("ERROR: LA_EMAIL_WU and LA_PASSWORD_WU must be set in .env")
    sys.exit(1)

print("Opening browser for WashU SSO login...")
print("If Duo MFA is required, approve the push on your phone.")
print()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context()
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
        page.fill("#ucWUSTLKeyLogin_txtUsername", EMAIL)
        page.fill("#ucWUSTLKeyLogin_txtPassword", PASSWORD)
        page.click("#ucWUSTLKeyLogin_btnLogin")
        print("Credentials submitted. Waiting for MFA / redirect (up to 3 min)...")
    except PWTimeout:
        print("Could not find WashU login form. The page may have changed.")
        print(f"Current URL: {page.url}")
        print("Complete login manually in the browser window, then press Enter here.")
        input()

    # Step 4: wait for successful login (mynotebook URL or Duo page)
    try:
        page.wait_for_url("**/mynotebook.labarchives.com/**", timeout=180_000)
    except PWTimeout:
        print("Did not reach mynotebook.labarchives.com within 3 minutes.")
        print(f"Current URL: {page.url}")
        print("Complete login manually, then press Enter here.")
        input()

    print(f"Logged in! Current URL: {page.url}")

    # Step 5: collect all LabArchives cookies
    all_cookies = ctx.cookies()
    la_cookies = [c for c in all_cookies if "labarchives.com" in c["domain"]]

    if not la_cookies:
        print("WARNING: No labarchives.com cookies found after login.")
    else:
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in la_cookies)

        # Auto-update .env — replace existing LA_SESSION_COOKIE line
        env_path = "C:/Users/axelr/OneDrive/Desktop/lab-agent-single-report/.env"
        with open(env_path, "r", encoding="utf-8") as f:
            env_text = f.read()

        import re
        new_line = f"LA_SESSION_COOKIE={cookie_str}"
        if "LA_SESSION_COOKIE=" in env_text:
            env_text = re.sub(r"LA_SESSION_COOKIE=.*", new_line, env_text)
        else:
            env_text += f"\n{new_line}\n"

        with open(env_path, "w", encoding="utf-8") as f:
            f.write(env_text)

        print()
        print("LA_SESSION_COOKIE updated in .env automatically.")

    browser.close()
