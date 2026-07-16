"""
LabArchives image extraction and download.

Two kinds of images appear on a page:

- **Attachment entries** — carry their own pre-signed download-url
  (no extra auth needed): parsed by ``parse_image_entries``.
- **Embedded ``<img>`` tags** inside rich-text entry HTML — may require plain
  GET, an HMAC-signed GET, or a web-session-cookie GET, tried in that order:
  handled by ``ImageDownloadMixin._download_image``.

``ImageDownloadMixin`` is mixed into ``LabArchivesAdapter`` and uses its
credential/base-url state (``_base_url``, ``_akid``, ``_password``).
"""
from __future__ import annotations

import base64
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .auth import ensure_session_cookies, is_base64, sign_request

# Image types we can download and Claude can read via the Read tool.
IMAGE_CONTENT_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp",
})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
CT_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def parse_image_entries(xml_text: str) -> list[tuple[str, str, str]]:
    """Return [(filename, download_url, caption), …] for image attachment entries.

    An entry is treated as an image if its content-type matches a known image
    MIME type, or if its filename has a recognised image extension.
    Only entries that carry a download-url are returned (pre-signed CDN links
    that need no additional HMAC auth).
    """
    root = ET.fromstring(xml_text)
    results: list[tuple[str, str, str]] = []
    for entry in root.findall(".//entry"):
        filename = entry.findtext("filename") or ""
        content_type = (entry.findtext("content-type") or "").lower().split(";")[0].strip()
        ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""

        is_image = content_type in IMAGE_CONTENT_TYPES or ext in IMAGE_EXTENSIONS
        if not is_image:
            continue

        download_url = entry.findtext("download-url") or entry.findtext("url") or ""
        if not download_url or not download_url.startswith("http"):
            continue

        caption = entry.findtext("caption") or filename
        results.append((filename, download_url, caption.strip()))
    return results


def extract_embedded_img_urls(xml_text: str) -> list[str]:
    """Extract src URLs from <img> tags embedded in entry-data HTML fields.

    These are inline images inside rich-text entries, distinct from file
    attachment entries which carry their own download-url element.
    """
    root = ET.fromstring(xml_text)
    urls: list[str] = []
    seen: set[str] = set()
    for entry in root.findall(".//entry"):
        html = entry.findtext("entry-data") or ""
        if not html or "<img" not in html.lower():
            continue
        for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE):
            src = m.group(1).strip()
            if src and src not in seen:
                urls.append(src)
                seen.add(src)
    return urls


def method_from_url(url: str) -> str:
    """Extract the LabArchives API method name from a URL path.

    For paths like /attachments/inline_image/<base64_id>, the method is
    'inline_image' (the second-to-last segment), not the base64 ID at the end.
    For other paths, falls back to the last non-empty segment.
    """
    path_parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
    # If the last segment looks like base64 (long, mixed chars), use the one before it
    if len(path_parts) >= 2 and len(path_parts[-1]) > 20 and is_base64(path_parts[-1]):
        return path_parts[-2]
    return path_parts[-1] if path_parts else "download"


class ImageDownloadMixin:
    """Image download methods for LabArchivesAdapter.

    Expects the host class to provide ``_base_url``, ``_akid``, ``_password``,
    and the session-cookie cache attributes set in ``__init__``.
    """

    _session_cookies: dict[str, str] | None
    _session_login_attempted: bool

    def _download_bytes(self, url: str) -> bytes:
        """Download raw bytes from a pre-signed URL (no HMAC auth needed)."""
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "lab-agent/1.0")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Image download error {exc.code}: {exc.reason}\n{body}"
            ) from exc

    def _ensure_session_cookies(self) -> dict[str, str] | None:
        """Return cached LabArchives session cookies (resolved once per adapter)."""
        if self._session_login_attempted:
            return self._session_cookies
        self._session_login_attempted = True
        self._session_cookies = ensure_session_cookies()
        return self._session_cookies

    def _download_image(self, url: str) -> tuple[bytes, str]:
        """Download an image URL. Returns (bytes, content_type).

        Handles data URIs (base64-encoded inline images) directly — no HTTP request.
        Handles relative URLs by prepending the base URL.
        Attempt order:
          1. Plain unauthenticated GET
          2. HMAC-signed GET (for API-hosted images)
          3. Session-cookie GET (for inline images that require web-session auth)
        """
        # data URIs are self-contained — decode the base64 directly, no HTTP needed.
        if url.startswith("data:"):
            try:
                header, _, b64data = url.partition(",")
                content_type = header.split(";")[0].replace("data:", "").strip() or "image/png"
                return base64.b64decode(b64data), content_type
            except Exception as exc:
                raise RuntimeError(f"Failed to decode data URI: {exc}") from exc

        if url.startswith("/"):
            url = self._base_url + url
        elif not url.startswith("http"):
            url = self._base_url + "/" + url.lstrip("/")

        def _try_fetch(fetch_url: str, cookies: dict[str, str] | None = None) -> tuple[bytes, str] | None:
            """Return (bytes, content_type) or None if the response is HTML (auth redirect)."""
            req = urllib.request.Request(fetch_url)
            req.add_header("User-Agent", "lab-agent/1.0")
            if cookies:
                req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()))
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                    ct = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
                    if ct == "text/html":
                        return None  # soft auth redirect
                    return data, ct
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    return None  # auth failure — try next method
                body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Image download error {exc.code}: {exc.reason}\n{body}"
                ) from exc

        # Attempt 1: plain GET
        result = _try_fetch(url)
        if result:
            return result

        # Attempt 2: HMAC-signed GET
        parsed = urllib.parse.urlparse(url)
        method = method_from_url(url)
        auth = sign_request(self._akid, self._password, method)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        params.update(auth)
        hmac_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(params)))
        result = _try_fetch(hmac_url)
        if result:
            return result

        # Attempt 3: WashU SSO session cookies
        session_cookies = self._ensure_session_cookies()
        if session_cookies:
            result = _try_fetch(url, cookies=session_cookies)
            if result:
                return result
            # Cookies returned HTML — they have expired
            raise RuntimeError(
                f"Image download failed for URL: {url}\n"
                "Session cookies appear to have expired (server returned an HTML login page).\n"
                "Run:  python get_la_cookies.py\n"
                "then paste the new LA_SESSION_COOKIE= line into your .env file and retry."
            )

        raise RuntimeError(
            f"Image download failed for URL: {url}\n"
            "No session cookies available. Run:  python get_la_cookies.py\n"
            "then paste the LA_SESSION_COOKIE= line into your .env file and retry."
        )
