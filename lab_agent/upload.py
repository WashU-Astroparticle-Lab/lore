"""
lab_agent/upload.py — upload experiment_report.md to LabArchives.

Creates a new page inside the "AI Agent" folder (Qubit & KID notebook) and
posts the report content as a rich-text HTML entry, then attaches the raw .md
file.

Uses the same sync urllib + HMAC-SHA512 auth as lab_agent/sources/labarchives.py.
"""
from __future__ import annotations

import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import markdown as _md

from .sources.labarchives import _sign_request

_BASE_URL = "https://api.labarchives.com"
_AI_AGENT_FOLDER_NAME = "AI Agent"
_TARGET_NOTEBOOK_NAME = "Qubit & KID"


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

    auth = _sign_request(akid, password, method)

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

    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _get_adapter():
    """Return a minimal LabArchivesAdapter for tree traversal (read-only)."""
    from .sources.labarchives import LabArchivesAdapter
    return LabArchivesAdapter("dummy")


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------

def _find_ai_agent_folder(adapter) -> tuple[str, str]:
    """Return (nbid, folder_tree_id) for the AI Agent folder.

    Searches all notebooks for a top-level folder named 'AI Agent'.
    Raises RuntimeError if not found.
    """
    notebooks = adapter._list_notebooks()
    for nb in notebooks:
        if nb["name"] != _TARGET_NOTEBOOK_NAME:
            continue
        for node in adapter._get_tree_level(nb["nbid"], "0"):
            label = node.findtext("display-text") or ""
            if label.strip().lower() == _AI_AGENT_FOLDER_NAME.lower():
                tree_id = node.findtext("tree-id") or ""
                return nb["nbid"], tree_id
    raise RuntimeError(
        f"Could not find '{_AI_AGENT_FOLDER_NAME}' folder in '{_TARGET_NOTEBOOK_NAME}' notebook. "
        "Please create it in LabArchives first."
    )


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

def _inline_images(html: str, out_dir: Path) -> str:
    """Replace local <img src="..."> paths with base64 data URIs.

    The markdown renderer produces relative paths (e.g. `labarchives_images/foo.png`
    or `github_images/bar.png`).  LabArchives cannot reach those local paths, so
    we embed each image's bytes directly into the HTML as a data URI.
    Paths that are already data URIs or absolute URLs are left untouched.
    Missing files are skipped with a warning.
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

    def _replace(match: re.Match) -> str:
        src = match.group(1)
        # Leave data URIs and http(s) URLs alone
        if src.startswith("data:") or src.startswith("http"):
            return match.group(0)
        img_path = out_dir / src
        if not img_path.exists():
            print(f"[upload] Warning: image not found, skipping inline: {img_path}")
            return match.group(0)
        # Resize to max 900px wide and compress as JPEG to keep payload small
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
            # Fallback: embed raw bytes
            ext = img_path.suffix.lower()
            mime = _EXT_TO_MIME.get(ext, "image/png")
            img_bytes = img_path.read_bytes()
        b64 = base64.b64encode(img_bytes).decode("ascii")
        return f'src="data:{mime};base64,{b64}"'

    return re.sub(r'src="([^"]+)"', _replace, html)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def upload_report(out_dir: Path, page_title: str) -> str:
    """Upload experiment_report.md to the AI Agent folder in LabArchives.

    Steps:
      1. Find the AI Agent folder in Qubit & KID.
      2. Create a new page named *page_title* inside it.
      3. Convert the report Markdown to HTML and post as a rich-text entry.
      4. Attach the raw .md file.

    Returns the new page's tree_id.
    """
    # Find the report file — prefer [UNSIGNED] *.md, fall back to experiment_report.md
    # Note: can't use glob("[UNSIGNED]*") — brackets are treated as character classes
    unsigned_files = sorted(f for f in out_dir.glob("*.md") if f.name.startswith("[UNSIGNED]"))
    report_path = unsigned_files[0] if unsigned_files else out_dir / "experiment_report.md"
    if not report_path.exists():
        raise FileNotFoundError(f"Report not found: {report_path}")

    md_text = report_path.read_text(encoding="utf-8")

    # Convert Markdown → HTML (tables, fenced code, etc.)
    html = _md.markdown(
        md_text,
        extensions=["tables", "fenced_code", "nl2br"],
    )

    # Inline all local images as base64 data URIs so they render in LabArchives.
    # The Markdown renderer produces <img src="relative/path.png"> tags; we
    # replace each src with a data URI by reading the file relative to out_dir.
    html = _inline_images(html, out_dir)

    print(f"[upload] Locating '{_AI_AGENT_FOLDER_NAME}' folder in LabArchives…")
    adapter = _get_adapter()
    nbid, folder_tree_id = _find_ai_agent_folder(adapter)
    print(f"[upload] Found folder. Creating page: {page_title!r}")

    page_tree_id = _insert_page(nbid, folder_tree_id, page_title)
    print(f"[upload] Page created (tree_id={page_tree_id[:40]}…)")

    print("[upload] Posting report as rich-text entry…")
    _add_text_entry(nbid, page_tree_id, html)

    print("[upload] Attaching raw .md file…")
    _add_attachment(nbid, page_tree_id, report_path)

    print(f"[upload] Done — report uploaded to LabArchives / AI Agent / {page_title!r}")
    return page_tree_id
