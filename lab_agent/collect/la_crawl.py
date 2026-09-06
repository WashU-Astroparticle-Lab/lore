"""Crawl LabArchives notebook pages into the local knowledge corpus (Stage 5a).

Walks every notebook's page tree via the adapter's low-level API (HMAC-authed — no
session cookies needed for text), extracts each page's text entries, and writes one
OKF ``Lab Page`` concept per page to ``knowledge/labarchives/<page>.md`` with
provenance (notebook, tree_id, entry timestamps). This is the corpus the Q&A search
(grep now, LightRAG later) indexes so *any* LabArchives page — not just ones LORE has
reported on — resolves ambiguous requests.

Images are out of scope for v1 (they need session cookies + a vision pass); text only.

Usage (via the build_kb CLI):
    python -m lab_agent.cli.build_kb [--notebook NAME] [--max-pages N]
"""
from __future__ import annotations

import json
import re

from ..config import KNOWLEDGE_ROOT, lab_config_value
from ..sources.labarchives import page_index
from ..sources.labarchives.adapter import LabArchivesAdapter
from ..sources.labarchives.images import extract_embedded_img_urls
from .okf import frontmatter


def _enumerate_pages(adapter, nbid: str, parent: str = "0", depth: int = 0,
                     out: list | None = None, exclude: set[str] | None = None
                     ) -> list[tuple[str, str]]:
    """Recursively collect (display_title, tree_id) for every page in a notebook.

    *exclude* is a set of lowercased folder display-texts to skip entirely (e.g. the
    "AI Agent" upload folder — we don't index LORE's own reports). One retry on a
    transient tree-level failure so a hiccup doesn't silently drop a whole subtree.
    """
    if out is None:
        out = []
    exclude = exclude or set()
    if depth > 12:
        return out
    try:
        nodes = adapter._get_tree_level(nbid, parent)
    except Exception:
        try:
            nodes = adapter._get_tree_level(nbid, parent)   # one retry
        except Exception:
            return out
    for node in nodes:
        b64 = node.findtext("tree-id") or ""
        display = (node.findtext("display-text") or "").strip()
        is_page = node.findtext("is-page") == "true"
        if is_page:
            if display and b64:
                out.append((display, b64))
        elif b64 and display.lower() not in exclude:   # don't descend into excluded folders
            _enumerate_pages(adapter, nbid, b64, depth + 1, out, exclude)
    return out


def crawl(adapter=None, notebooks: list[str] | None = None,
          max_pages: int | None = None, exclude_folders: set[str] | None = None,
          images_out: dict | None = None) -> list[dict]:
    """Return [{notebook, page, nbid, tree_id, text}, …] for LabArchives pages.

    *adapter* is injected for testing; in production a real LabArchivesAdapter is used
    only for its authenticated low-level methods. *notebooks* filters by name (the KG
    is scoped to the primary notebook); *exclude_folders* skips folders by display-text
    (e.g. the AI Agent upload folder); *max_pages* caps pages fetched.
    """
    adapter = adapter or LabArchivesAdapter("__crawl__")
    exclude = {f.strip().lower() for f in (exclude_folders or set()) if f.strip()}
    nbs = adapter._list_notebooks()
    if notebooks:
        wanted = {n.strip().lower() for n in notebooks}
        nbs = [nb for nb in nbs if nb.get("name", "").strip().lower() in wanted]

    corpus: list[dict] = []
    seen_pages: list[tuple[str, str, str]] = []  # (display, nbid, tree_id) for page_index
    for nb in nbs:
        nbid = nb["nbid"]
        for display, tree_id in _enumerate_pages(adapter, nbid, exclude=exclude):
            if max_pages is not None and len(corpus) >= max_pages:
                break
            seen_pages.append((display, nbid, tree_id))
            try:
                xml = adapter._get_entries_for_page(nbid, tree_id)
                entries = adapter._parse_entries_xml(xml)
            except Exception as exc:
                print(f"[la_crawl] Skipping page {display!r}: {exc}")
                continue
            # Tier 1: record this page's embedded images (URLs only — no fetch, no cookie),
            # keyed by the same safe-name as the la_page doc so Tier 2 can map KG hit -> figures.
            if images_out is not None:
                imgs = extract_embedded_img_urls(xml)
                if imgs:
                    images_out[_safe(display)] = {
                        "page": display, "nbid": nbid, "tree_id": tree_id, "images": imgs,
                    }
            text = "\n\n".join(e["content"] for e in entries if e.get("content"))
            if text.strip():
                corpus.append({
                    "notebook": nb.get("name", ""), "page": display,
                    "nbid": nbid, "tree_id": tree_id, "text": text,
                })
    # Record everything we saw so future title lookups skip the tree walk.
    try:
        page_index.record_pages(seen_pages)
    except Exception:
        pass
    return corpus


def _safe(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_")[:60] or "page"


def write_corpus(corpus: list[dict]) -> int:
    """Write each crawled page as an OKF Lab Page concept; return count written."""
    la_dir = KNOWLEDGE_ROOT / "labarchives"
    la_dir.mkdir(parents=True, exist_ok=True)
    for doc in corpus:
        fm = frontmatter(
            "Lab Page",
            resource=doc["page"],
            tags=[t for t in (doc.get("notebook"),) if t],
        )
        body = f"# {doc['page']}\n\n_Notebook: {doc.get('notebook', '')} · tree_id: {doc['tree_id']}_\n\n{doc['text']}"
        (la_dir / f"{_safe(doc['page'])}.md").write_text(fm + body, encoding="utf-8")
    return len(corpus)


IMAGE_MANIFEST_PATH = KNOWLEDGE_ROOT / "labarchives_images.json"


def write_image_manifest(images_by_page: dict) -> int:
    """Write the Tier-1 per-page image manifest; return total image count.

    Maps ``{safe_page_name: {page, nbid, tree_id, images:[urls]}}`` — the safe name
    matches the ``la_page:<name>`` KG doc, so once a query resolves to a page, Tier 2
    can look up its figures here and fetch them on demand (cookie-gated).
    """
    IMAGE_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    total = sum(len(v.get("images", [])) for v in images_by_page.values())
    IMAGE_MANIFEST_PATH.write_text(
        json.dumps({"pages": images_by_page}, indent=2), encoding="utf-8")
    return total


def crawl_to_bundle(notebooks: list[str] | None = None, max_pages: int | None = None,
                    exclude_folders: set[str] | None = None) -> int:
    """Crawl LabArchives and write the corpus into knowledge/labarchives/. Returns count.

    Defaults: notebooks -> the lab_config "Primary notebook" (KG is scoped to it);
    exclude_folders -> the lab_config "Upload folder" (the AI Agent folder — LORE's own
    reports are kept out of the graph).
    """
    if notebooks is None:
        primary = lab_config_value("Primary notebook").strip()
        notebooks = [primary] if primary else None
    if exclude_folders is None:
        upload = lab_config_value("Upload folder").strip()
        exclude_folders = {upload} if upload else set()
    images_out: dict = {}
    corpus = crawl(notebooks=notebooks, max_pages=max_pages,
                   exclude_folders=exclude_folders, images_out=images_out)
    n = write_corpus(corpus)
    n_imgs = write_image_manifest(images_out)
    print(f"[la_crawl] image manifest: {len(images_out)} page(s) with {n_imgs} image(s)")
    return n
