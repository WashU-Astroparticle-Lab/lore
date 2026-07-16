"""
Persistent LabArchives page index: normalized page title → (nbid, tree_id).

Title resolution otherwise walks every notebook tree one get_tree_level call
per folder — dozens of API round trips per lookup, repeated on every run.
Every traversal already visits many pages, so we record all of them here and
consult the index first on later runs. Entries are validated by actually
fetching the page, so stale entries (moved/renamed pages) simply fall back
to a fresh traversal, which rewrites them.

The index lives in cache/la_page_index.json (gitignored).
"""
from __future__ import annotations

import json
import threading
import time

from ...config import PROJECT_ROOT

INDEX_PATH = PROJECT_ROOT / "cache" / "la_page_index.json"
_lock = threading.Lock()


def normalize_title(title: str) -> str:
    return " ".join(title.strip().lower().split())


def lookup(title: str) -> tuple[str, str] | None:
    """Return (nbid, tree_id) for a cached page title, or None."""
    with _lock:
        try:
            data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
    entry = data.get("pages", {}).get(normalize_title(title))
    if entry and entry.get("nbid") and entry.get("tree_id"):
        return entry["nbid"], entry["tree_id"]
    return None


def record_pages(pages: list[tuple[str, str, str]]) -> None:
    """Merge (display_title, nbid, b64_tree_id) tuples into the index."""
    rows = [(d, n, t) for d, n, t in pages if d and n and t]
    if not rows:
        return
    with _lock:
        try:
            data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        index = data.setdefault("pages", {})
        now = time.time()
        for display, nbid, tree_id in rows:
            index[normalize_title(display)] = {
                "display": display,
                "nbid": nbid,
                "tree_id": tree_id,
                "cached_at": now,
            }
        try:
            INDEX_PATH.parent.mkdir(exist_ok=True)
            INDEX_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"[LabArchives] Warning: could not write page index: {exc}")
