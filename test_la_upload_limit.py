"""
test_la_upload_limit.py — Binary-search the LabArchives rich-text entry size limit.

Creates a throwaway test page in the AI Agent folder, posts entries of increasing
size until a 413 is returned, then binary-searches the threshold.

Cleans up the test page at the end (best-effort).

Usage:
    python test_la_upload_limit.py

Output: prints the approximate maximum payload size LabArchives will accept.
"""
from __future__ import annotations

import base64
import io
import os
import sys
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent.resolve()
load_dotenv(_PROJECT_ROOT / ".env", override=True)

sys.path.insert(0, str(_PROJECT_ROOT))
from lab_agent.upload import (
    _get_adapter,
    _find_ai_agent_folder,
    _insert_page,
    _add_text_entry,
)

def _make_html_with_images(n_images: int, kb_each: int = 50) -> tuple[str, int]:
    """Build an HTML string with n_images synthetic base64-encoded JPEG images.

    Returns (html, total_raw_compressed_bytes).
    """
    try:
        from PIL import Image

        buf = io.BytesIO()
        img = Image.new("RGB", (400, 300), color=(100, 149, 237))
        img.save(buf, format="JPEG", quality=82)
        one_img_bytes = buf.getvalue()
    except ImportError:
        # Fallback: use repeating bytes as synthetic JPEG-ish payload
        one_img_bytes = b"\xff\xd8\xff" + b"X" * (kb_each * 1024)

    b64 = base64.b64encode(one_img_bytes).decode("ascii")
    data_uri = f"data:image/jpeg;base64,{b64}"

    imgs_html = "".join(
        f'<p>Image {i + 1}:</p><img src="{data_uri}" />\n'
        for i in range(n_images)
    )
    html = f"<h1>Upload limit test — {n_images} images</h1>\n{imgs_html}"

    # Estimate the actual HTTP body size (URL-encoded form POST)
    # The image data URIs are already in the html; urlencode adds overhead.
    body_bytes = urllib.parse.urlencode({"entry_data": html}).encode("utf-8")
    return html, len(body_bytes)


def _try_post(nbid: str, page_tree_id: str, html: str) -> bool:
    """Attempt to post a rich-text entry. Returns True on success, False on 413."""
    try:
        _add_text_entry(nbid, page_tree_id, html)
        return True
    except RuntimeError as exc:
        if "413" in str(exc):
            return False
        raise


def main() -> None:
    print("[test] Connecting to LabArchives…")
    adapter = _get_adapter()
    nbid, folder_tree_id = _find_ai_agent_folder(adapter)
    print(f"[test] Found AI Agent folder. Creating test page…")
    page_tree_id = _insert_page(nbid, folder_tree_id, "_upload_limit_test_DELETE_ME")
    print(f"[test] Test page created (tree_id={page_tree_id[:40]}…)")

    try:
        # Phase 1: exponential probe — double images until 413
        n = 1
        last_ok = 0
        first_fail = None
        print("\n[test] Phase 1: exponential probe (doubling image count)")
        while n <= 512:
            html, body_bytes = _make_html_with_images(n)
            ok = _try_post(nbid, page_tree_id, html)
            status = "OK " if ok else "413"
            print(f"  {n:4d} images  body~{body_bytes / 1024:.0f} KB  [{status}]")
            if ok:
                last_ok = n
            else:
                first_fail = n
                break
            n *= 2

        if first_fail is None:
            print(f"\n[test] No 413 up to {last_ok} images. LabArchives limit is higher than tested.")
            return

        # Phase 2: binary search between last_ok and first_fail
        print(f"\n[test] Phase 2: binary search between {last_ok} and {first_fail} images")
        lo, hi = last_ok, first_fail
        while hi - lo > 1:
            mid = (lo + hi) // 2
            html, body_bytes = _make_html_with_images(mid)
            ok = _try_post(nbid, page_tree_id, html)
            status = "OK " if ok else "413"
            print(f"  {mid:4d} images  body~{body_bytes / 1024:.0f} KB  [{status}]")
            if ok:
                lo = mid
            else:
                hi = mid

        # Final result
        html_ok, body_ok = _make_html_with_images(lo)
        html_fail, body_fail = _make_html_with_images(hi)
        raw_ok_kb = int(body_ok / 1.41 / 1024)
        print(f"\n[test] === RESULT ===")
        print(f"  Max images that fit : {lo}  (HTTP body ~ {body_ok // 1024} KB)")
        print(f"  First count to 413  : {hi}  (HTTP body ~ {body_fail // 1024} KB)")
        print(f"  LabArchives limit   : between {body_ok // 1024} KB and {body_fail // 1024} KB URL-encoded body")
        print(f"  _MAX_INLINE_BYTES recommendation: {raw_ok_kb} KB raw")
        print(f"  (body KB / 1.41 overhead = raw KB; set cap slightly below this)")

    finally:
        print(f"\n[test] Done. Manually delete the test page '_upload_limit_test_DELETE_ME' from LabArchives / AI Agent.")


if __name__ == "__main__":
    main()
