from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from ..models import CollectedArtifact

_DEFAULT_BASE_URL = "https://api.labarchives.com"

# Image types we can download and Claude can read via the Read tool.
_IMAGE_CONTENT_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp",
})
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_CT_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


# ---------------------------------------------------------------------------
# URL / ref parsing
# ---------------------------------------------------------------------------

def _is_base64(s: str) -> bool:
    """Return True if s looks like a base64-encoded string (not a plain URL or number)."""
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9+/=]+", s)) and len(s) >= 8 and not s.isdigit()


def _parse_entry_ref(entry_ref: str) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Return (base_url, b64_page_tree_id, known_nbid, numeric_id, page_title).

    Accepted forms:
      - "12345678"
          bare numeric tree_id; search all notebooks
      - raw base64 tree_id (e.g. "MjM1LjN8MTE1Mj...")
          used directly as page_tree_id; search all notebooks
      - "https://…?…&tree_id=12345678…"
          query-param style numeric ID
      - "https://mynotebook.labarchives.com/<nbid>/page/<num>[-<suffix>]"
          direct notebook URL — nbid extracted from path; numeric page ID for search
      - "https://mynotebook.labarchives.com/share/<name>/<b64>/page/<num>[-<suffix>]"
          legacy share-link — b64 segment before /page/ tried as page tree_id
      - "20260303 Amplitude & DAC Sweep"  (plain text, not numeric/base64/URL)
          page title search — traverses all notebooks matching display-text
    """
    entry_ref = entry_ref.strip()

    # Bare numeric ID
    if entry_ref.isdigit():
        return _DEFAULT_BASE_URL, None, None, entry_ref, None

    # URL forms
    if entry_ref.startswith("http"):
        parsed = urllib.parse.urlparse(entry_ref)

        # Query-param style: ?tree_id=…
        qs = urllib.parse.parse_qs(parsed.query)
        tree_ids = qs.get("tree_id") or qs.get("nb_id")
        if tree_ids:
            return _DEFAULT_BASE_URL, None, None, tree_ids[0], None

        path_parts = [p for p in parsed.path.split("/") if p]
        if "page" not in path_parts:
            raise ValueError(
                f"Cannot parse LabArchives entry ref: {entry_ref!r}\n"
                "Expected a bare numeric tree_id, a URL with tree_id=… in the query string, "
                "or a mynotebook.labarchives.com URL with /page/<id> in the path."
            )

        page_idx = path_parts.index("page")
        page_segment = path_parts[page_idx + 1]   # e.g. "11400322-163"
        numeric_id = page_segment.split("-")[0]
        if not numeric_id.isdigit():
            raise ValueError(f"Cannot parse page ID from URL segment: {page_segment!r}")

        # Direct notebook URL: /<nbid>/page/<num>  (no "share" prefix)
        # The segment immediately before /page/ is the base64-encoded notebook ID (nbid).
        if "share" not in path_parts and page_idx > 0:
            candidate_nbid = path_parts[page_idx - 1]
            try:
                base64.b64decode(candidate_nbid + "==")
                # It's a valid base64 string — treat as nbid
                return _DEFAULT_BASE_URL, None, candidate_nbid, numeric_id, None
            except Exception:
                pass

        # Legacy share-link: /share/<name>/<b64_page_tree_id>/page/<num>
        # The segment before /page/ (after "share" and name) is the page tree_id.
        b64_page_tid: str | None = None
        if page_idx > 0:
            candidate = path_parts[page_idx - 1]
            try:
                base64.b64decode(candidate + "==")
                b64_page_tid = candidate
            except Exception:
                pass

        return _DEFAULT_BASE_URL, b64_page_tid, None, numeric_id, None

    # Raw base64 tree_id (not a URL, not all digits)
    if _is_base64(entry_ref):
        return _DEFAULT_BASE_URL, entry_ref, None, None, None

    # Plain text page title — search notebooks by display-text
    return _DEFAULT_BASE_URL, None, None, None, entry_ref


# ---------------------------------------------------------------------------
# HMAC-SHA512 request signing
# ---------------------------------------------------------------------------

def _sign_request(akid: str, password: str, method: str) -> dict[str, str]:
    """Return auth query parameters for a signed LabArchives API request."""
    expires = str(int(time.time() * 1000) + 120_000)
    message = f"{akid}{method}{expires}".encode()
    digest = hmac.new(password.encode("utf-8"), message, hashlib.sha512).digest()
    sig = base64.b64encode(digest).decode("ascii")
    return {"akid": akid, "expires": expires, "sig": sig}


# ---------------------------------------------------------------------------
# Session cookie extraction from Chrome (for inline images)
# ---------------------------------------------------------------------------

def _get_chrome_labarchives_cookies() -> dict[str, str] | None:
    """Extract existing LabArchives session cookies from the Chrome browser.

    Reads cookies for labarchives.com directly from Chrome's cookie store
    using browser_cookie3. This reuses the user's active browser session —
    no new login, no session conflict.

    Returns a {name: value} dict on success, or None if Chrome has no
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


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        import re
        return re.sub(r"\s+", " ", "".join(self._parts)).strip()


def _html_to_text(html: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class LabArchivesAdapter:
    """Fetch a LabArchives page and return its entries as CollectedArtifacts.

    Authentication
    --------------
    Reads credentials from environment variables:

      LA_AKID      Access Key ID  (e.g. "Washington_StL_TeWgHN")
      LA_SECRET    Institutional API password — HMAC-SHA512 signing key
      LA_UID       Permanent numeric user ID
      LA_BASE_URL  Optional API host override

    How entry refs are resolved
    ---------------------------
    1. If a share-link URL is given, the base64 page tree_id is extracted
       directly from the path.
    2. Otherwise, each of the user's notebooks is traversed (get_tree_level)
       to find a page whose decoded tree_id contains the numeric target ID.
    3. Once the page tree_id and notebook ID are known, get_entries_for_page
       is called to retrieve the page content.
    """

    def __init__(
        self,
        entry_ref: str,
        akid: str | None = None,
        password: str | None = None,
        uid: str | None = None,
    ) -> None:
        self._akid = akid or os.environ.get("LA_AKID") or ""
        # LA_SECRET is the institutional API credential (HMAC signing key).
        self._password = (
            password
            or os.environ.get("LA_SECRET")
            or os.environ.get("LA_PASSWORD")
            or ""
        )
        self._uid = uid or os.environ.get("LA_UID") or ""
        env_base = os.environ.get("LA_BASE_URL") or ""
        self._base_url = env_base.rstrip("/") if env_base else _DEFAULT_BASE_URL
        _base, self._b64_tree_id, self._known_nbid, self._numeric_tree_id, self._page_title = _parse_entry_ref(entry_ref)
        # Session cookies for inline image downloads — lazily populated on first use.
        self._session_cookies: dict[str, str] | None = None
        self._session_login_attempted: bool = False

    # ------------------------------------------------------------------
    # Low-level API helpers
    # ------------------------------------------------------------------

    def _get(self, endpoint: str, method: str, extra_params: dict[str, str]) -> str:
        auth = _sign_request(self._akid, self._password, method)
        params = urllib.parse.urlencode({**extra_params, **auth})
        url = f"{self._base_url}{endpoint}?{params}"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "lab-agent/1.0")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"LabArchives API error {exc.code} ({endpoint}): {exc.reason}\n{body}"
            ) from exc

    def _list_notebooks(self) -> list[dict[str, str]]:
        """Return [{nbid, name}, …] for all notebooks owned by the user."""
        xml_text = self._get(
            "/api/users/user_info_via_id",
            "user_info_via_id",
            {"uid": self._uid},
        )
        root = ET.fromstring(xml_text)
        return [
            {
                "nbid": nb.findtext("id") or "",
                "name": nb.findtext("name") or "",
            }
            for nb in root.findall(".//notebook")
            if nb.findtext("id")
        ]

    def _get_tree_level(self, nbid: str, parent_tree_id: str = "0") -> list[ET.Element]:
        xml_text = self._get(
            "/api/tree_tools/get_tree_level",
            "get_tree_level",
            {"uid": self._uid, "nbid": nbid, "parent_tree_id": parent_tree_id},
        )
        return ET.fromstring(xml_text).findall(".//level-node")

    def _find_page_tree_id(self, nbid: str, numeric_target: str, parent: str = "0", depth: int = 0) -> str | None:
        """Recursively search the notebook tree for a page containing numeric_target."""
        if depth > 5:
            return None
        try:
            nodes = self._get_tree_level(nbid, parent)
        except RuntimeError:
            return None
        for node in nodes:
            b64_tid = node.findtext("tree-id") or ""
            is_page = node.findtext("is-page") == "true"
            try:
                decoded = base64.b64decode(b64_tid + "==").decode("utf-8", errors="replace")
            except Exception:
                decoded = ""
            if numeric_target in decoded:
                return b64_tid
            if not is_page:
                result = self._find_page_tree_id(nbid, numeric_target, b64_tid, depth + 1)
                if result:
                    return result
        return None

    def _find_page_by_title(self, nbid: str, title: str, parent: str = "0", depth: int = 0) -> str | None:
        """Recursively search the notebook tree for a page whose display-text matches title (case-insensitive)."""
        if depth > 5:
            return None
        try:
            nodes = self._get_tree_level(nbid, parent)
        except RuntimeError:
            return None
        title_lower = title.lower()
        for node in nodes:
            b64_tid = node.findtext("tree-id") or ""
            display = (node.findtext("display-text") or "").strip()
            is_page = node.findtext("is-page") == "true"
            if is_page and display.lower() == title_lower:
                return b64_tid
            if not is_page:
                result = self._find_page_by_title(nbid, title, b64_tid, depth + 1)
                if result:
                    return result
        return None

    def _get_entries_for_page(self, nbid: str, page_tree_id: str) -> str:
        """Call get_entries_for_page and return raw XML."""
        print(f"[LabArchives] Fetching page tree_id={page_tree_id[:20]}… from notebook={nbid[:20]}…")
        return self._get(
            "/api/tree_tools/get_entries_for_page",
            "get_entries_for_page",
            {
                "uid": self._uid,
                "nbid": nbid,
                "page_tree_id": page_tree_id,
                "entry_data": "true",
            },
        )

    # ------------------------------------------------------------------
    # XML parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_entries_xml(xml_text: str) -> list[tuple[str, str]]:
        """Return [(caption_or_type, plain_text_content), …] for each entry."""
        root = ET.fromstring(xml_text)
        results: list[tuple[str, str]] = []
        for entry in root.findall(".//entry"):
            label = (
                entry.findtext("caption")
                or entry.findtext("filename")
                or entry.findtext("part-type")
                or "entry"
            )
            raw = entry.findtext("entry-data") or ""
            content = _html_to_text(raw) if raw and "<" in raw else raw
            if content:
                results.append((label.strip(), content.strip()))
        return results

    @staticmethod
    def _parse_image_entries(xml_text: str) -> list[tuple[str, str, str]]:
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

            is_image = content_type in _IMAGE_CONTENT_TYPES or ext in _IMAGE_EXTENSIONS
            if not is_image:
                continue

            download_url = entry.findtext("download-url") or entry.findtext("url") or ""
            if not download_url or not download_url.startswith("http"):
                continue

            caption = entry.findtext("caption") or filename
            results.append((filename, download_url, caption.strip()))
        return results

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

    @staticmethod
    def _extract_embedded_img_urls(xml_text: str) -> list[str]:
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

    def _ensure_session_cookies(self) -> dict[str, str] | None:
        """Return cached LabArchives session cookies.

        Priority:
          1. LA_SESSION_COOKIE env var  (e.g. "session_key=abc; other=xyz")
          2. browser_cookie3 extraction from Edge/Chrome/Firefox
        """
        if self._session_login_attempted:
            return self._session_cookies
        self._session_login_attempted = True

        # 1. Explicit env var — highest priority, works everywhere
        raw = os.environ.get("LA_SESSION_COOKIE", "").strip()
        if raw:
            cookies: dict[str, str] = {}
            for part in raw.split(";"):
                part = part.strip()
                if "=" in part:
                    k, _, v = part.partition("=")
                    cookies[k.strip()] = v.strip()
            if cookies:
                print(f"[LabArchives] Using {len(cookies)} cookie(s) from LA_SESSION_COOKIE env var.")
                self._session_cookies = cookies
                return self._session_cookies

        # 2. Fallback: read from browser cookie store
        self._session_cookies = _get_chrome_labarchives_cookies()
        return self._session_cookies

    @staticmethod
    def _method_from_url(url: str) -> str:
        """Extract the LabArchives API method name from a URL path.

        For paths like /attachments/inline_image/<base64_id>, the method is
        'inline_image' (the second-to-last segment), not the base64 ID at the end.
        For other paths, falls back to the last non-empty segment.
        """
        path_parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
        # If the last segment looks like base64 (long, mixed chars), use the one before it
        if len(path_parts) >= 2 and len(path_parts[-1]) > 20 and _is_base64(path_parts[-1]):
            return path_parts[-2]
        return path_parts[-1] if path_parts else "download"

    def _download_image(self, url: str) -> tuple[bytes, str]:
        """Download an image URL. Returns (bytes, content_type).

        Handles relative URLs by prepending the base URL.
        Attempt order:
          1. Plain unauthenticated GET
          2. HMAC-signed GET (for API-hosted images)
          3. Session-cookie GET (for inline images that require web-session auth)
        """
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
        method = self._method_from_url(url)
        auth = _sign_request(self._akid, self._password, method)
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

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch(self) -> list[CollectedArtifact]:
        """Fetch the LabArchives page and return its entries as CollectedArtifacts."""
        # Step 1: resolve page tree_id and notebook ID.
        page_tree_id: str | None = None
        nbid: str | None = None
        probe_xml: str | None = None  # set by b64 branch to avoid a second fetch

        # Determine which notebooks to search.
        if self._known_nbid:
            notebooks = [{"nbid": self._known_nbid, "name": ""}]
        else:
            notebooks = self._list_notebooks()

        if self._b64_tree_id:
            # Raw base64 tree_id passed directly — try it as page_tree_id.
            page_tree_id = self._b64_tree_id
            for nb in notebooks:
                try:
                    probe_xml = self._get_entries_for_page(nb["nbid"], page_tree_id)
                    nbid = nb["nbid"]
                    break
                except RuntimeError:
                    continue
            if nbid is None:
                raise RuntimeError(
                    f"Page b64_tree_id={self._b64_tree_id[:20]}… "
                    f"not found in any of the {len(notebooks)} searched notebook(s). "
                    "It may belong to a shared/external notebook."
                )
        elif self._page_title:
            # Page title: search notebooks by display-text.
            print(f"[LabArchives] Searching for page titled {self._page_title!r}…")
            for nb in notebooks:
                found = self._find_page_by_title(nb["nbid"], self._page_title)
                if found:
                    page_tree_id = found
                    nbid = nb["nbid"]
                    break
            if page_tree_id is None or nbid is None:
                nb_desc = f"notebook {self._known_nbid[:20]}…" if self._known_nbid else f"{len(notebooks)} notebook(s)"
                raise RuntimeError(
                    f"No page titled {self._page_title!r} found in {nb_desc}. "
                    "Check the exact page title in LabArchives (case-insensitive match)."
                )
        else:
            # Numeric ID: search notebooks by tree traversal.
            for nb in notebooks:
                found = self._find_page_tree_id(nb["nbid"], self._numeric_tree_id)
                if found:
                    page_tree_id = found
                    nbid = nb["nbid"]
                    break
            if page_tree_id is None or nbid is None:
                nb_desc = f"notebook {self._known_nbid[:20]}…" if self._known_nbid else f"{len(notebooks)} notebook(s)"
                raise RuntimeError(
                    f"No page containing numeric ID {self._numeric_tree_id!r} "
                    f"found in {nb_desc}. "
                    "The web URL's page ID may not directly map to the API tree_id. "
                    "Try passing the base64 tree_id directly (e.g. from list_notebook_pages)."
                )

        # Step 2: fetch entries (reuse probe result for b64 inputs to avoid a double fetch)
        xml_text = probe_xml if probe_xml is not None else self._get_entries_for_page(nbid, page_tree_id)
        entries = self._parse_entries_xml(xml_text)

        ref = self._page_title or self._b64_tree_id or self._numeric_tree_id

        # Build text artifacts
        if not entries:
            text_artifacts: list[CollectedArtifact] = [
                CollectedArtifact(
                    path=f"labarchives://{ref}",
                    kind="notes",
                    description="LabArchives page (no text entries)",
                    exists=True,
                    content=None,
                    source="labarchives",
                )
            ]
        else:
            text_artifacts = [
                CollectedArtifact(
                    path=f"labarchives://{ref}/{i}",
                    kind="notes",
                    description=f"LabArchives entry: {label}",
                    exists=True,
                    content=content,
                    source="labarchives",
                )
                for i, (label, content) in enumerate(entries)
            ]

        # Slugify the ref for use in filenames (avoids collisions across pages)
        safe_ref = re.sub(r"[^\w]", "_", str(ref))[:40].strip("_")

        # Build image artifacts — attachment entries first
        image_artifacts: list[CollectedArtifact] = []
        for filename, download_url, caption in self._parse_image_entries(xml_text):
            print(f"[LabArchives] Downloading attachment image: {filename}")
            try:
                raw = self._download_bytes(download_url)
                image_artifacts.append(CollectedArtifact(
                    path=f"labarchives://{ref}/{filename}",
                    kind="figure",
                    description=f"LabArchives image: {caption}",
                    exists=True,
                    raw_bytes=raw,
                    source="labarchives",
                ))
            except RuntimeError as exc:
                print(f"[LabArchives] Warning: skipping attachment {filename} — {exc}")

        # Embedded <img> images inside rich-text entry HTML
        for i, img_url in enumerate(self._extract_embedded_img_urls(xml_text), start=1):
            print(f"[LabArchives] Downloading embedded image {i}: {img_url[:70]}…")
            try:
                raw, content_type = self._download_image(img_url)
                ext = _CT_TO_EXT.get(content_type, ".png")
                filename = f"{safe_ref}_img_{i}{ext}"
                image_artifacts.append(CollectedArtifact(
                    path=f"labarchives://{ref}/{filename}",
                    kind="figure",
                    description=f"LabArchives embedded image {i} (from page: {ref})",
                    exists=True,
                    raw_bytes=raw,
                    source="labarchives",
                ))
            except RuntimeError as exc:
                print(f"[LabArchives] Warning: skipping embedded image {i} — {exc}")

        return text_artifacts + image_artifacts
