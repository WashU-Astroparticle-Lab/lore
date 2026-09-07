"""
lab_agent/publish/labarchives.py — upload a finished report to LabArchives.

Creates a new page inside the configured upload folder (see lab_config.md:
`Upload folder` / `Primary notebook`) and posts the report content as a
rich-text HTML entry, then attaches the raw .md file.

Uses the same sync urllib + HMAC-SHA512 auth as lab_agent/sources/labarchives.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import datetime as _dt
import json as _json
import re as _re

import markdown as _md

from ..config import lab_config_value
from ..sources.labarchives import sign_request

_BASE_URL = "https://api.labarchives.com"
# Fallbacks used when lab_config.md is absent or missing these keys.
_DEFAULT_FOLDER_NAME = "AI Agent"
_DEFAULT_NOTEBOOK_NAME = "Qubit & KID"


def _upload_folder_name() -> str:
    return lab_config_value("Upload folder", _DEFAULT_FOLDER_NAME)


def _target_notebook_name() -> str:
    return lab_config_value("Primary notebook", _DEFAULT_NOTEBOOK_NAME)


# ---------------------------------------------------------------------------
# Low-level POST helper
# ---------------------------------------------------------------------------

def _post(
    endpoint: str,
    method: str,
    params: dict[str, str],
    body: bytes = b"",
    content_type: str = "application/octet-stream",
    form_body: bool = False,
) -> str:
    """Sign and POST to a LabArchives API endpoint.

    By default, all params go in the query string and body carries binary data
    (for attachment uploads).  When form_body=True, the payload params are sent
    as a URL-encoded POST body instead (avoids HTTP 414 for large entry_data).
    Auth params always go in the query string.
    """
    akid = os.environ.get("LA_AKID", "")
    password = os.environ.get("LA_SECRET", "")
    uid = os.environ.get("LA_UID", "")

    auth = sign_request(akid, password, method)

    if form_body:
        # Auth + uid in query string; payload in POST body as form data
        qs = urllib.parse.urlencode({"uid": uid, **auth})
        url = f"{_BASE_URL}{endpoint}?{qs}"
        body = urllib.parse.urlencode(params).encode("utf-8")
        content_type = "application/x-www-form-urlencoded"
    else:
        all_params = {**params, "uid": uid, **auth}
        qs = urllib.parse.urlencode(all_params)
        url = f"{_BASE_URL}{endpoint}?{qs}"

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("User-Agent", "lab-agent/1.0")
    if body:
        req.add_header("Content-Type", content_type)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"LabArchives API HTTP {exc.code} on {endpoint}: {body}"
        ) from exc


def _get_adapter():
    """Return a minimal LabArchivesAdapter for tree traversal (read-only)."""
    from ..sources.labarchives import LabArchivesAdapter
    return LabArchivesAdapter("dummy")


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------

def _find_upload_folder(adapter) -> tuple[str, str]:
    """Return (nbid, folder_tree_id) for the configured upload folder.

    Searches the configured primary notebook for a top-level folder with the
    configured upload-folder name. Raises RuntimeError if not found.
    """
    folder_name = _upload_folder_name()
    notebook_name = _target_notebook_name()
    notebooks = adapter._list_notebooks()
    for nb in notebooks:
        if nb["name"] != notebook_name:
            continue
        for node in adapter._get_tree_level(nb["nbid"], "0"):
            label = node.findtext("display-text") or ""
            if label.strip().lower() == folder_name.lower():
                tree_id = node.findtext("tree-id") or ""
                return nb["nbid"], tree_id
    raise RuntimeError(
        f"Could not find '{folder_name}' folder in '{notebook_name}' notebook. "
        "Please create it in LabArchives first."
    )


def _find_page_in_folder(adapter, nbid: str, folder_tree_id: str, title: str) -> str | None:
    """Return the tree_id of an existing page named *title*, or None.

    Without this, every upload creates a new page, so re-uploading a revised
    report leaves several pages sharing one title and no way to tell which is
    current.
    """
    want = title.strip().lower()
    try:
        nodes = adapter._get_tree_level(nbid, folder_tree_id)
    except Exception as exc:
        print(f"[upload] Warning: could not list existing pages ({exc}); will create a new page.")
        return None
    for node in nodes:
        label = (node.findtext("display-text") or "").strip().lower()
        if label == want:
            return node.findtext("tree-id") or None
    return None


def _notebook_url(nbid: str) -> str:
    """Notebook-level LabArchives URL.

    Deliberately not a deep link to the page: LabArchives' web page IDs do not
    map to API tree_ids (see CLAUDE.md 'Known limitation'), so a per-page URL
    cannot be constructed from what the API returns. Naming the folder and page
    title alongside this link is honest and still gets a reader there in two
    clicks — better than inventing a URL that 404s.
    """
    return f"https://mynotebook.labarchives.com/{nbid}"


def _insert_page(nbid: str, parent_tree_id: str, title: str) -> str:
    """Create a new page and return its tree_id."""
    xml_text = _post(
        "/api/tree_tools/insert_node",
        "insert_node",
        {
            "nbid": nbid,
            "parent_tree_id": parent_tree_id,
            "display_text": title,
            "is_folder": "false",
        },
    )
    root = ET.fromstring(xml_text)
    # Check for API error
    err = root.findtext(".//error-code")
    if err:
        raise RuntimeError(
            f"insert_node failed ({err}): {root.findtext('.//error-description')}\n{xml_text}"
        )
    node = root.find(".//node")
    if node is None:
        raise RuntimeError(f"insert_node returned no <node>:\n{xml_text}")
    return node.findtext("tree-id") or ""


def _add_text_entry(nbid: str, page_tree_id: str, html: str, caption: str = "") -> str:
    """Post an HTML rich-text entry to a page; return entry id."""
    params: dict[str, str] = {
        "nbid": nbid,
        "pid": page_tree_id,
        "part_type": "text entry",
        "entry_data": html,
    }
    if caption:
        params["caption"] = caption
    xml_text = _post("/api/entries/add_entry", "add_entry", params, form_body=True)
    root = ET.fromstring(xml_text)
    err = root.findtext(".//error-code")
    if err:
        raise RuntimeError(
            f"add_entry failed ({err}): {root.findtext('.//error-description')}\n{xml_text}"
        )
    entry = root.find(".//entry")
    return entry.findtext("eid") if entry is not None else ""


def _add_attachment(nbid: str, page_tree_id: str, file_path: Path) -> str:
    """Upload a file as an attachment; return entry id."""
    file_bytes = file_path.read_bytes()
    params: dict[str, str] = {
        "nbid": nbid,
        "pid": page_tree_id,
        "filename": file_path.name,
    }
    xml_text = _post(
        "/api/entries/add_attachment",
        "add_attachment",
        params,
        body=file_bytes,
        content_type="application/octet-stream",
    )
    root = ET.fromstring(xml_text)
    err = root.findtext(".//error-code")
    if err:
        raise RuntimeError(
            f"add_attachment failed ({err}): {root.findtext('.//error-description')}\n{xml_text}"
        )
    entry = root.find(".//entry")
    return entry.findtext("eid") if entry is not None else ""


# ---------------------------------------------------------------------------
# Image inlining
# ---------------------------------------------------------------------------

_MAX_INLINE_BYTES = 640 * 1024  # 640 KB raw compressed → ~900 KB URL-encoded body (LabArchives limit ~1 MB, verified empirically)


def _inline_images(html: str, out_dir: Path) -> tuple[str, list[str]]:
    """Replace local <img src="..."> paths with base64 data URIs.

    Stops inlining once the total raw compressed image payload reaches
    _MAX_INLINE_BYTES. Images beyond the cap are reported as skipped.

    Returns (html_with_inlined_images, list_of_skipped_image_paths).
    """
    import base64
    import re

    _EXT_TO_MIME = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }

    missing: list[str] = []
    total_inlined = 0

    def _replace(match: re.Match) -> str:
        nonlocal total_inlined
        src = match.group(1)
        if src.startswith("data:") or src.startswith("http"):
            return match.group(0)
        img_path = out_dir / src
        if not img_path.exists():
            missing.append(src)
            return match.group(0)
        try:
            from PIL import Image
            import io
            with Image.open(img_path) as im:
                im = im.convert("RGB")
                max_w = 900
                if im.width > max_w:
                    ratio = max_w / im.width
                    im = im.resize((max_w, int(im.height * ratio)), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=82, optimize=True)
                img_bytes = buf.getvalue()
            mime = "image/jpeg"
        except Exception:
            ext = img_path.suffix.lower()
            mime = _EXT_TO_MIME.get(ext, "image/png")
            img_bytes = img_path.read_bytes()
        if total_inlined + len(img_bytes) > _MAX_INLINE_BYTES:
            missing.append(f"{src} (payload cap — see attached .md for full report)")
            return match.group(0)
        total_inlined += len(img_bytes)
        b64 = base64.b64encode(img_bytes).decode("ascii")
        return f'src="data:{mime};base64,{b64}"'

    result = re.sub(r'src="([^"]+)"', _replace, html)
    return result, missing


# ---------------------------------------------------------------------------
# Overflow image entries
# ---------------------------------------------------------------------------

def _post_overflow_images(nbid: str, page_tree_id: str, overflow_srcs: list[str], out_dir: Path) -> int:
    """Post images that exceeded the inline cap as individual rich-text entries.

    Each entry contains one resized JPEG (same resize logic as _inline_images) so
    it is always well under the 640 KB LabArchives body limit.  Returns the count
    of images successfully posted.
    """
    import base64

    _EXT_TO_MIME = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }

    posted = 0
    for src in overflow_srcs:
        img_path = out_dir / src
        if not img_path.exists():
            print(f"[upload] Overflow image not found: {src}", flush=True)
            continue
        try:
            from PIL import Image
            import io
            with Image.open(img_path) as im:
                im = im.convert("RGB")
                if im.width > 900:
                    ratio = 900 / im.width
                    im = im.resize((900, int(im.height * ratio)), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=82, optimize=True)
                img_bytes = buf.getvalue()
            mime = "image/jpeg"
        except Exception:
            ext = img_path.suffix.lower()
            mime = _EXT_TO_MIME.get(ext, "image/png")
            img_bytes = img_path.read_bytes()

        b64 = base64.b64encode(img_bytes).decode("ascii")
        entry_html = (
            f'<p><strong>{img_path.name}</strong></p>'
            f'<p><img src="data:{mime};base64,{b64}"></p>'
        )
        _add_text_entry(nbid, page_tree_id, entry_html)
        posted += 1

    return posted


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve_report_path(out_dir: Path, report_file: str | Path | None = None) -> Path:
    """Pick the report to upload.

    *report_file* (absolute, or relative to *out_dir*) wins when given — an
    output directory can hold several reports (e.g. a full one and a
    plain-language one), and "newest [UNSIGNED] by mtime" silently picks the
    wrong one.
    """
    if report_file:
        path = Path(report_file)
        if not path.is_absolute():
            path = out_dir / path
        if not path.exists():
            raise FileNotFoundError(f"Report not found: {path}")
        return path

    # Note: can't use glob("[UNSIGNED]*") — brackets are treated as character classes
    unsigned_files = sorted(
        (f for f in out_dir.glob("*.md") if f.name.startswith("[UNSIGNED]")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    path = unsigned_files[0] if unsigned_files else out_dir / "experiment_report.md"
    if not path.exists():
        raise FileNotFoundError(f"Report not found: {path}")
    if len(unsigned_files) > 1:
        print(
            f"[upload] {len(unsigned_files)} reports in {out_dir.name}/ — picking the newest "
            f"({path.name}). Pass --report-file to choose explicitly."
        )
    return path


def upload_report(
    out_dir: Path,
    page_title: str,
    report_file: str | Path | None = None,
    new_page: bool = False,
) -> dict:
    """Upload the report in *out_dir* to the configured LabArchives folder.

    Steps:
      1. Find the upload folder (lab_config.md `Upload folder`) in the
         primary notebook (lab_config.md `Primary notebook`).
      2. Reuse the page named *page_title* if it already exists there, else
         create it. (Pass ``new_page=True`` to always create.)
      3. Convert the report Markdown to HTML and post as a rich-text entry.
      4. Attach the raw .md file.

    Returns a dict describing where the report landed: ``page_tree_id``,
    ``page_title``, ``folder``, ``notebook_url``, ``created`` and ``report_file``.
    Callers need this to tell the reader where to look — previously only a
    tree_id came back, which is not something a human can follow.
    """
    report_path = resolve_report_path(out_dir, report_file)

    md_text = report_path.read_text(encoding="utf-8")

    # Convert Markdown → HTML (tables, fenced code, etc.)
    html_orig = _md.markdown(
        md_text,
        extensions=["tables", "fenced_code", "nl2br"],
    )

    # Inline local images as base64 data URIs up to _MAX_INLINE_BYTES cap.
    html, missing_images = _inline_images(html_orig, out_dir)

    # Identify images skipped due to payload cap (have relative src paths remaining).
    overflow_srcs = [
        m.group(1) for m in _re.finditer(r'src="([^"]+)"', html)
        if not m.group(1).startswith("data:") and not m.group(1).startswith("http")
    ]

    if missing_images:
        cap_skipped = [p for p in missing_images if "payload cap" in p]
        missing_only = [p for p in missing_images if "payload cap" not in p]
        if cap_skipped:
            print(
                f"[upload] {len(cap_skipped)} image(s) exceeded inline cap — will post as separate entries.",
                flush=True,
            )
        if missing_only:
            print(
                f"[upload] {len(missing_only)} image(s) not found (skipped):\n"
                + "\n".join(f"  - {p}" for p in missing_only[:10]),
                flush=True,
            )

    print(f"[upload] Locating '{_upload_folder_name()}' folder in LabArchives…")
    adapter = _get_adapter()
    nbid, folder_tree_id = _find_upload_folder(adapter)

    existing = None if new_page else _find_page_in_folder(adapter, nbid, folder_tree_id, page_title)
    if existing:
        page_tree_id = existing
        created = False
        print(f"[upload] Page {page_title!r} already exists — adding a revision to it "
              "(use new_page=True to create a duplicate instead).")
        # LabArchives entries are append-only through this API, so a revision is
        # posted as a new entry rather than replacing the old one. The header
        # makes it obvious which entry on the page is current.
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        html = f"<p><strong>Revised {stamp}</strong></p>\n" + html
    else:
        created = True
        print(f"[upload] Creating page: {page_title!r}")
        page_tree_id = _insert_page(nbid, folder_tree_id, page_title)
        print(f"[upload] Page created (tree_id={page_tree_id[:40]}…)")

    print("[upload] Posting report as rich-text entry…")
    try:
        _add_text_entry(nbid, page_tree_id, html)
    except RuntimeError as exc:
        if "413" in str(exc):
            print("[upload] 413 — payload too large; retrying text-only…", flush=True)
            html_no_img = _re.sub(r'<img[^>]+>', '[image — see figure entries below]', html_orig)
            _add_text_entry(nbid, page_tree_id, html_no_img)
            print("[upload] Text-only entry posted.", flush=True)
            # All images become overflow in this case
            overflow_srcs = [
                m.group(1) for m in _re.finditer(r'src="([^"]+)"', html_orig)
                if not m.group(1).startswith("data:") and not m.group(1).startswith("http")
            ]
        else:
            raise

    # Post images that didn't fit in the main entry as individual figure entries.
    if overflow_srcs:
        print(f"[upload] Posting {len(overflow_srcs)} overflow image(s) as separate entries…", flush=True)
        n_posted = _post_overflow_images(nbid, page_tree_id, overflow_srcs, out_dir)
        print(f"[upload] Posted {n_posted} additional figure entries.", flush=True)

    print("[upload] Attaching raw .md file…")
    _add_attachment(nbid, page_tree_id, report_path)

    folder = _upload_folder_name()
    result = {
        "page_tree_id": page_tree_id,
        "page_title": page_title,
        "folder": folder,
        "notebook_url": _notebook_url(nbid),
        "created": created,
        "report_file": report_path.name,
    }
    verb = "uploaded to" if created else "revised on"
    print(f"[upload] Done — {report_path.name} {verb} LabArchives / {folder} / {page_title!r}")
    print(f"[upload] Notebook: {result['notebook_url']}  (page: {folder} / {page_title})")

    # Record where this landed so later sessions can cite the location instead of
    # guessing a URL.
    meta_path = out_dir / "metadata.json"
    if meta_path.exists():
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            meta["labarchives_upload"] = result
            meta_path.write_text(_json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[upload] Warning: could not record upload location in metadata.json ({exc})")

    return result
