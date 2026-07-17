"""
LabArchives page adapter: entry-ref resolution, notebook tree traversal, and
page fetching. Auth primitives live in ``auth.py``; image extraction and
download in ``images.py``.
"""
from __future__ import annotations

import base64
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

from ...config import lab_config_value
from ...models import CollectedArtifact
from . import page_index
from .auth import DEFAULT_BASE_URL, is_base64, sign_request
from .images import (
    CT_TO_EXT,
    ImageDownloadMixin,
    extract_embedded_img_urls,
    parse_image_entries,
)


# ---------------------------------------------------------------------------
# URL / ref parsing
# ---------------------------------------------------------------------------

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
        return DEFAULT_BASE_URL, None, None, entry_ref, None

    # URL forms
    if entry_ref.startswith("http"):
        parsed = urllib.parse.urlparse(entry_ref)

        # Query-param style: ?tree_id=…
        qs = urllib.parse.parse_qs(parsed.query)
        tree_ids = qs.get("tree_id") or qs.get("nb_id")
        if tree_ids:
            return DEFAULT_BASE_URL, None, None, tree_ids[0], None

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
                return DEFAULT_BASE_URL, None, candidate_nbid, numeric_id, None
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

        return DEFAULT_BASE_URL, b64_page_tid, None, numeric_id, None

    # Raw base64 tree_id (not a URL, not all digits)
    if is_base64(entry_ref):
        return DEFAULT_BASE_URL, entry_ref, None, None, None

    # Plain text page title — search notebooks by display-text
    return DEFAULT_BASE_URL, None, None, None, entry_ref


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith("http"):
                self._href = href
                self._parts.append("[")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self._parts.append(f"]({self._href})")
            self._href = None

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        parts = self._parts
        # If an <a href="..."> was opened but </a> was never seen (malformed HTML),
        # _href is still set and there's a dangling '[' with no closing ']'.
        # Remove it so the output isn't broken Markdown.
        if self._href is not None:
            for i in range(len(parts) - 1, -1, -1):
                if parts[i] == "[":
                    parts.pop(i)
                    break
        return re.sub(r"\s+", " ", "".join(parts)).strip()


def _html_to_text(html: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class LabArchivesAdapter(ImageDownloadMixin):
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
        self._base_url = env_base.rstrip("/") if env_base else DEFAULT_BASE_URL
        _base, self._b64_tree_id, self._known_nbid, self._numeric_tree_id, self._page_title = _parse_entry_ref(entry_ref)
        # Session cookies for inline image downloads — lazily populated on first use.
        self._session_cookies: dict[str, str] | None = None
        self._session_login_attempted: bool = False

    # ------------------------------------------------------------------
    # Low-level API helpers
    # ------------------------------------------------------------------

    def _get(self, endpoint: str, method: str, extra_params: dict[str, str]) -> str:
        auth = sign_request(self._akid, self._password, method)
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

    def _find_display_title_for_tree_id(self, nbid: str, target_tree_id: str, parent: str = "0", depth: int = 0) -> str | None:
        """Traverse the notebook tree and return the display-text for the node with target_tree_id."""
        if depth > 6:
            return None
        try:
            nodes = self._get_tree_level(nbid, parent)
        except RuntimeError:
            return None
        for node in nodes:
            b64_tid = node.findtext("tree-id") or ""
            display = (node.findtext("display-text") or "").strip()
            is_page = node.findtext("is-page") == "true"
            if b64_tid == target_tree_id:
                return display or None
            if not is_page:
                result = self._find_display_title_for_tree_id(nbid, target_tree_id, b64_tid, depth + 1)
                if result:
                    return result
        return None

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

    def _find_page_by_title(
        self,
        nbid: str,
        title: str,
        parent: str = "0",
        depth: int = 0,
        _collected: "list[tuple[str, str, str]] | None" = None,
    ) -> str | None:
        """Recursively search for a page by title.

        Matching order:
          1. Exact case-insensitive match
          2. Collapsed-whitespace case-insensitive match (handles extra spaces, etc.)

        _collected accumulates (display_text, nbid, b64_tid) for every page encountered
        so the caller can do fallback matching and build useful error messages.
        """
        if depth > 6:
            return None
        try:
            nodes = self._get_tree_level(nbid, parent)
        except RuntimeError:
            return None

        title_exact = title.strip().lower()
        title_norm = " ".join(title_exact.split())

        for node in nodes:
            b64_tid = node.findtext("tree-id") or ""
            display = (node.findtext("display-text") or "").strip()
            is_page = node.findtext("is-page") == "true"

            if is_page:
                if _collected is not None:
                    _collected.append((display, nbid, b64_tid))
                d_exact = display.lower()
                d_norm = " ".join(d_exact.split())
                if d_exact == title_exact or d_norm == title_norm:
                    return b64_tid

            if not is_page:
                result = self._find_page_by_title(nbid, title, b64_tid, depth + 1, _collected)
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

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch(self, include_images: bool = True) -> list[CollectedArtifact]:
        """Fetch the LabArchives page and return its entries as CollectedArtifacts.

        With ``include_images=False`` only text entries are returned — no image
        downloads and no session-cookie requirement.
        """
        # Step 1: resolve page tree_id and notebook ID.
        page_tree_id: str | None = None
        nbid: str | None = None
        probe_xml: str | None = None  # set by b64 branch to avoid a second fetch

        # Determine which notebooks to search. Most pages live in the lab's
        # primary notebook (lab_config.md), so search it first — stable sort
        # keeps the original order for the rest.
        if self._known_nbid:
            notebooks = [{"nbid": self._known_nbid, "name": ""}]
        else:
            notebooks = self._list_notebooks()
            primary = lab_config_value("Primary notebook").strip().lower()
            if primary:
                notebooks.sort(key=lambda nb: 0 if nb["name"].strip().lower() == primary else 1)

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
            # Resolve human-readable title for filename generation (avoids cryptic b64 prefix).
            display_title = self._find_display_title_for_tree_id(nbid, page_tree_id)
            if display_title:
                self._page_title = display_title
                print(f"[LabArchives] Resolved b64 tree_id to display title: {display_title!r}")
        elif self._page_title:
            # Fast path: consult the persistent page index first. A hit is
            # validated by actually fetching the page, so a stale entry
            # (moved/renamed page) just falls through to the full traversal.
            cached = page_index.lookup(self._page_title)
            if cached:
                cached_nbid, cached_tid = cached
                try:
                    probe_xml = self._get_entries_for_page(cached_nbid, cached_tid)
                    nbid, page_tree_id = cached_nbid, cached_tid
                    print(f"[LabArchives] Page index hit for {self._page_title!r}")
                except RuntimeError:
                    print(f"[LabArchives] Page index entry for {self._page_title!r} "
                          "is stale — re-searching…")

        if page_tree_id is None and self._page_title and not self._b64_tree_id:
            # Page title: search notebooks by display-text.
            print(f"[LabArchives] Searching for page titled {self._page_title!r}…")
            all_pages: list[tuple[str, str, str]] = []  # (display, nbid, b64_tid)
            for nb in notebooks:
                found = self._find_page_by_title(nb["nbid"], self._page_title, _collected=all_pages)
                if found:
                    page_tree_id = found
                    nbid = nb["nbid"]
                    break

            # Every page seen during the traversal goes into the index so
            # later runs resolve titles without walking the tree again.
            page_index.record_pages(all_pages)

            if page_tree_id is None or nbid is None:
                # Fallback: if the title starts with an 8-digit date and exactly one page
                # in the notebook shares that date prefix, use it automatically. This
                # recovers from minor punctuation mangling (e.g. a comma in the wrong place).
                date_prefix = self._page_title[:8] if len(self._page_title) >= 8 and self._page_title[:8].isdigit() else ""
                if date_prefix:
                    prefix_hits = [(d, n, t) for d, n, t in all_pages if d.startswith(date_prefix)]
                    if len(prefix_hits) == 1:
                        display_match, nbid_match, tid_match = prefix_hits[0]
                        print(f"[LabArchives] Fuzzy date-prefix match: using {display_match!r} "
                              f"(searched for {self._page_title!r})")
                        page_tree_id = tid_match
                        nbid = nbid_match
                        self._page_title = display_match  # use real title for filenames

            if page_tree_id is None or nbid is None:
                nb_desc = f"notebook {self._known_nbid[:20]}…" if self._known_nbid else f"{len(notebooks)} notebook(s)"
                date_prefix = self._page_title[:8] if len(self._page_title) >= 8 and self._page_title[:8].isdigit() else ""
                similar_displays = [d for d, _, _ in all_pages if date_prefix and d.startswith(date_prefix)]
                if not similar_displays:
                    similar_displays = [d for d, _, _ in all_pages[:20]]
                hint = (
                    "\n\nPages available in the notebook:\n" + "\n".join(f"  · {p!r}" for p in similar_displays)
                    if similar_displays else ""
                )
                raise RuntimeError(
                    f"No page titled {self._page_title!r} found in {nb_desc}. "
                    "Check the exact page title in LabArchives (case-insensitive match)."
                    + hint
                )
        if page_tree_id is None and self._numeric_tree_id and not self._page_title:
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

        if not include_images:
            return text_artifacts

        # Slugify the ref for use in filenames (avoids collisions across pages)
        safe_ref = re.sub(r"[^\w]", "_", str(ref))[:40].strip("_")

        # Build image artifacts — downloads are independent, so run them
        # concurrently; pool.map preserves entry order so artifact ordering
        # (and hence manifest/report ordering) matches the sequential layout.
        attachment_entries = parse_image_entries(xml_text)
        embedded_urls = list(enumerate(extract_embedded_img_urls(xml_text), start=1))

        def _fetch_attachment(item: tuple[str, str, str]) -> CollectedArtifact | None:
            filename, download_url, caption = item
            print(f"[LabArchives] Downloading attachment image: {filename}")
            try:
                raw = self._download_bytes(download_url)
                return CollectedArtifact(
                    path=f"labarchives://{ref}/{filename}",
                    kind="figure",
                    description=f"LabArchives image: {caption}",
                    exists=True,
                    raw_bytes=raw,
                    source="labarchives",
                )
            except RuntimeError as exc:
                print(f"[LabArchives] Warning: skipping attachment {filename} — {exc}")
                return None

        def _fetch_embedded(item: tuple[int, str]) -> CollectedArtifact | str | None:
            """Return an artifact, a cookie-error message (str), or None (skipped)."""
            i, img_url = item
            print(f"[LabArchives] Downloading embedded image {i}: {img_url[:70]}…")
            try:
                raw, content_type = self._download_image(img_url)
                ext = CT_TO_EXT.get(content_type, ".png")
                filename = f"{safe_ref}_img_{i}{ext}"
                return CollectedArtifact(
                    path=f"labarchives://{ref}/{filename}",
                    kind="figure",
                    description=f"LabArchives embedded image {i} (from page: {ref})",
                    exists=True,
                    raw_bytes=raw,
                    source="labarchives",
                )
            except RuntimeError as exc:
                msg = str(exc)
                if "expired" in msg.lower() or "LA_SESSION_COOKIE" in msg or "get_la_cookies" in msg:
                    return msg
                print(f"[LabArchives] Warning: skipping embedded image {i} — {exc}")
                return None

        # Resolve session cookies once up front — _download_image lazily resolves
        # them on first need, and doing that inside the pool would race.
        if embedded_urls:
            self._ensure_session_cookies()

        image_artifacts: list[CollectedArtifact] = []
        cookie_errors: list[str] = []
        n_downloads = len(attachment_entries) + len(embedded_urls)
        if n_downloads:
            with ThreadPoolExecutor(max_workers=min(8, n_downloads)) as pool:
                attachment_results = pool.map(_fetch_attachment, attachment_entries)
                embedded_results = pool.map(_fetch_embedded, embedded_urls)
                image_artifacts.extend(a for a in attachment_results if a is not None)
                for result in embedded_results:
                    if isinstance(result, str):
                        cookie_errors.append(result)
                    elif result is not None:
                        image_artifacts.append(result)

        if cookie_errors:
            raise RuntimeError(
                f"COOKIE_REFRESH_NEEDED: LabArchives session cookies have expired "
                f"({len(cookie_errors)} embedded image(s) failed).\n"
                "Run:  python get_la_cookies.py\n"
                "Then retry the pipeline."
            )

        return text_artifacts + image_artifacts
