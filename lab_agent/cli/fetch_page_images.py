"""Fetch a LabArchives page's embedded figures to disk (Tier 2 — on-demand vision).

Given a page (its safe-name key from the image manifest, or its title), downloads that
page's embedded images locally so the agent can Read them with vision and answer questions
about a specific plot. The page is normally identified first by the knowledge-graph query
(`query_kb`), then its figures looked up here.

Embedded LabArchives images require the session cookie (Duo) — if it's expired this prints
COOKIE_REFRESH_NEEDED and exits 3 (run `python get_la_cookies.py`, then retry).

Usage: python -m lab_agent.cli.fetch_page_images "<page safe-name or title>"
"""
from __future__ import annotations

import json
import sys

from ..config import KNOWLEDGE_ROOT, load_env
from ..collect.la_crawl import IMAGE_MANIFEST_PATH, _safe
from ..sources.labarchives.adapter import LabArchivesAdapter
from ..sources.labarchives.images import CT_TO_EXT


def lookup_page(page: str, manifest_path=IMAGE_MANIFEST_PATH) -> dict | None:
    """Find a page record in the image manifest by exact key, safe-name, or title."""
    try:
        pages = json.loads(manifest_path.read_text(encoding="utf-8")).get("pages", {})
    except Exception:
        return None
    if page in pages:
        return pages[page]
    if _safe(page) in pages:
        return pages[_safe(page)]
    for rec in pages.values():
        if rec.get("page", "").strip().lower() == page.strip().lower():
            return rec
    return None


def main() -> None:
    load_env()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    page = " ".join(sys.argv[1:]).strip()
    rec = lookup_page(page)
    if not rec:
        print(f"No figures found for page {page!r} in the image manifest. "
              "(Run `python -m lab_agent.cli.build_kb` to (re)build it, or check the page name.)")
        sys.exit(1)

    # Bound the cache before adding to it. These figures are re-fetchable in
    # seconds (they just need a live cookie), so stale pages are not worth
    # keeping — and this tree sits inside OneDrive, which syncs every byte.
    from ..cache_prune import prune_quietly
    prune_quietly(KNOWLEDGE_ROOT / "image_cache")

    out = KNOWLEDGE_ROOT / "image_cache" / _safe(rec["page"])
    out.mkdir(parents=True, exist_ok=True)
    # Mark it in use: a page being fetched is a page being discussed, and the
    # follow-up question must not pay for a re-fetch.
    from ..retention import touch_used
    touch_used(out)
    adapter = LabArchivesAdapter("__fetch__")
    saved: list[str] = []
    for i, url in enumerate(rec["images"], 1):
        try:
            data, ct = adapter._download_image(url)
        except RuntimeError as exc:
            msg = str(exc)
            if "cookie" in msg.lower() or "get_la_cookies" in msg.lower():
                print("COOKIE_REFRESH_NEEDED: LabArchives session cookie expired. "
                      "Run:  python get_la_cookies.py   then retry.")
                sys.exit(3)
            print(f"  figure {i} failed: {exc}")
            continue
        path = out / f"fig_{i}{CT_TO_EXT.get(ct, '.png')}"
        path.write_bytes(data)
        saved.append(str(path))

    print(f"Fetched {len(saved)}/{len(rec['images'])} figure(s) for page {rec['page']!r}:")
    for s in saved:
        print(f"  {s}")

    # Flag figures too large for the vision API (~2000 px/side in multi-image reads) so the
    # reader downsizes/zooms them via `view_figure` instead of hitting a silent rejection.
    oversized: list[str] = []
    try:
        from PIL import Image
        for s in saved:
            if max(Image.open(s).size) > 1500:
                oversized.append(s)
    except Exception:
        pass
    if oversized:
        print(f"\n{len(oversized)} figure(s) exceed the vision size limit — before reading each, run:")
        for s in oversized:
            print(f"  python -m lab_agent.cli.view_figure \"{s}\"")

    print("\nRead these paths with the Read tool to view the figures and answer.")
    print("For a precise/quantitative read of a small plot detail, zoom first, e.g.:")
    print("  python -m lab_agent.cli.view_figure \"<fig path>\" --crop 0.47 0 1 1 --scale 2")


if __name__ == "__main__":
    main()
