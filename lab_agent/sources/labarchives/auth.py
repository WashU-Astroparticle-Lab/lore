"""
LabArchives authentication primitives.

Two independent auth paths coexist:

1. **HMAC-SHA512 signed API requests** (``sign_request``) — the institutional
   API credential (LA_AKID / LA_SECRET) signs every api.labarchives.com call.
2. **Web session cookies** — some inline images are only served to a logged-in
   browser session. Cookies come from the LA_SESSION_COOKIE env var (written by
   ``refresh_session_cookies`` / ``python get_la_cookies.py``) or, as a
   fallback, straight from an installed browser's cookie store.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time
import urllib.request

DEFAULT_BASE_URL = "https://api.labarchives.com"


def is_base64(s: str) -> bool:
    """Return True if s looks like a base64-encoded LabArchives tree_id.

    LabArchives tree_ids are base64 strings ≥ 20 characters.  Using a high
    minimum length prevents page titles like "Cooldown" (which happen to be
    all-alphanumeric) from being misidentified as tree_ids.
    """
    return bool(re.fullmatch(r"[A-Za-z0-9+/=]+", s)) and len(s) >= 20 and not s.isdigit()


# ---------------------------------------------------------------------------
# HMAC-SHA512 request signing
# ---------------------------------------------------------------------------

def sign_request(akid: str, password: str, method: str) -> dict[str, str]:
    """Return auth query parameters for a signed LabArchives API request."""
    expires = str(int(time.time() * 1000) + 120_000)
    message = f"{akid}{method}{expires}".encode()
    digest = hmac.new(password.encode("utf-8"), message, hashlib.sha512).digest()
    sig = base64.b64encode(digest).decode("ascii")
    return {"akid": akid, "expires": expires, "sig": sig}


# ---------------------------------------------------------------------------
# Session cookies
# ---------------------------------------------------------------------------

def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse a ``name=value; other=value`` cookie header string into a dict."""
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies


def get_browser_labarchives_cookies() -> dict[str, str] | None:
    """Extract existing LabArchives session cookies from an installed browser.

    Reads cookies for labarchives.com directly from the browser's cookie store
    using browser_cookie3. This reuses the user's active browser session —
    no new login, no session conflict.

    Returns a {name: value} dict on success, or None if no browser has
    LabArchives cookies (user not logged in) or the library is unavailable.
    """
    try:
        import browser_cookie3
    except ImportError:
        print("[LabArchives] browser_cookie3 not installed. "
              "Run: pip install browser-cookie3")
        return None

    # Try browsers in order: Edge, Chrome, Firefox
    browsers = [
        ("Edge",    browser_cookie3.edge),
        ("Chrome",  browser_cookie3.chrome),
        ("Firefox", browser_cookie3.firefox),
    ]
    for browser_name, browser_fn in browsers:
        try:
            jar = browser_fn(domain_name=".labarchives.com")
            cookies = {c.name: c.value for c in jar}
            if not cookies:
                jar = browser_fn(domain_name="labarchives.com")
                cookies = {c.name: c.value for c in jar}
            if cookies:
                print(f"[LabArchives] Loaded {len(cookies)} cookie(s) from {browser_name}.")
                return cookies
        except Exception:
            continue

    print("[LabArchives] No LabArchives cookies found in any browser. "
          "Make sure you are logged in to LabArchives in Edge, Chrome, or Firefox.")
    return None


def cookies_still_valid(cookie_str: str) -> bool:
    """Return True if the existing LA_SESSION_COOKIE can reach LabArchives."""
    if not cookie_str:
        return False
    try:
        req = urllib.request.Request(
            "https://mynotebook.labarchives.com/",
            headers={"Cookie": cookie_str, "User-Agent": "lab-agent/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            # A 200 that does NOT redirect to the login page means the session is live.
            final_url = resp.geturl()
            return "login" not in final_url and "auth-service" not in final_url
    except Exception:
        return False


def ensure_session_cookies() -> dict[str, str] | None:
    """Return LabArchives session cookies for image downloads.

    Priority:
      1. LA_SESSION_COOKIE env var  (e.g. "session_key=abc; other=xyz")
      2. browser_cookie3 extraction from Edge/Chrome/Firefox
    """
    raw = os.environ.get("LA_SESSION_COOKIE", "").strip()
    if raw:
        cookies = parse_cookie_header(raw)
        if cookies:
            print(f"[LabArchives] Using {len(cookies)} cookie(s) from LA_SESSION_COOKIE env var.")
            return cookies
    return get_browser_labarchives_cookies()
