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


def _la_image_id(url: str) -> str:
    """LabArchives' own id for an inline image, from its URL.

    The URL carries ``file_name=<id>.png``. That id is per-image and stable, so it
    survives the page being reordered — unlike the positional ``img_N`` names we
    otherwise fall back to. Returns "" when the parameter is absent.
    """
    import urllib.parse as _up

    q = _up.parse_qs(_up.urlparse(url).query)
    # Some manifests carry the param as "amp;file_name" (HTML-escaped &).
    for key in ("file_name", "amp;file_name"):
        if q.get(key):
            return q[key][0]
    return ""


def _enumerate_pages(adapter, nbid: str, parent: str = "0", depth: int = 0,
                     out: list | None = None, exclude: set[str] | None = None,
                     failures: list | None = None, include_within: set[str] | None = None
                     ) -> list[tuple[str, str]]:
    """Recursively collect (display_title, tree_id) for every page in a notebook.

    *exclude* is a set of lowercased folder display-texts to skip (e.g. the "AI Agent"
    upload folder — we don't index LORE's own reports). *include_within* names subfolders
    of an excluded folder that are crawled anyway: the Slack archive lives in the upload
    folder, and its week pages belong in the graph while the reports beside them do not.
    One retry on a transient tree-level failure so a hiccup doesn't silently drop a whole
    subtree; a subtree that still fails is logged and recorded in *failures*, so the crawl
    is known to be partial and nothing is pruned on its account.
    """
    if out is None:
        out = []
    exclude = exclude or set()
    include_within = include_within or set()
    if depth > 12:
        return out
    try:
        nodes = adapter._get_tree_level(nbid, parent)
    except Exception:
        try:
            nodes = adapter._get_tree_level(nbid, parent)   # one retry
        except Exception as exc:
            print(f"[la_crawl] could not list folder {parent!r} in notebook {nbid!r}: {exc}")
            if failures is not None:
                failures.append(parent)
            return out
    for node in nodes:
        b64 = node.findtext("tree-id") or ""
        display = (node.findtext("display-text") or "").strip()
        is_page = node.findtext("is-page") == "true"
        if is_page:
            if display and b64:
                out.append((display, b64))
        elif b64 and display.lower() not in exclude:
            _enumerate_pages(adapter, nbid, b64, depth + 1, out, exclude, failures,
                             include_within)
        elif b64 and include_within:
            # An excluded folder: skip its pages, crawl only the named subfolders in it.
            try:
                inner = adapter._get_tree_level(nbid, b64)
            except Exception as exc:
                print(f"[la_crawl] could not list folder {display!r}: {exc}")
                if failures is not None:
                    failures.append(b64)
                continue
            for sub in inner:
                name = (sub.findtext("display-text") or "").strip()
                sid = sub.findtext("tree-id") or ""
                if sid and sub.findtext("is-page") != "true" and name.lower() in include_within:
                    _enumerate_pages(adapter, nbid, sid, depth + 2, out, set(), failures)
    return out


def crawl(adapter=None, notebooks: list[str] | None = None,
          max_pages: int | None = None, exclude_folders: set[str] | None = None,
          images_out: dict | None = None, status_out: dict | None = None,
          include_within: set[str] | None = None) -> list[dict]:
    """Return [{notebook, page, nbid, tree_id, text}, …] for LabArchives pages.

    *adapter* is injected for testing; in production a real LabArchivesAdapter is used
    only for its authenticated low-level methods. *notebooks* filters by name (the KG
    is scoped to the primary notebook); *exclude_folders* skips folders by display-text
    (e.g. the AI Agent upload folder); *max_pages* caps pages fetched. *status_out*, if
    given, receives {"tree_failures": n, "fetch_failed": {safe names}, "truncated": bool}.
    """
    adapter = adapter or LabArchivesAdapter("__crawl__")
    exclude = {f.strip().lower() for f in (exclude_folders or set()) if f.strip()}
    within = {f.strip().lower() for f in (include_within or set()) if f.strip()}
    nbs = adapter._list_notebooks()
    if notebooks:
        wanted = {n.strip().lower() for n in notebooks}
        nbs = [nb for nb in nbs if nb.get("name", "").strip().lower() in wanted]

    corpus: list[dict] = []
    seen_pages: list[tuple[str, str, str]] = []  # (display, nbid, tree_id) for page_index
    failures: list = []
    fetch_failed: set[str] = set()
    truncated = False
    for nb in nbs:
        nbid = nb["nbid"]
        for display, tree_id in _enumerate_pages(adapter, nbid, exclude=exclude,
                                                 failures=failures, include_within=within):
            if max_pages is not None and len(corpus) >= max_pages:
                truncated = True
                break
            seen_pages.append((display, nbid, tree_id))
            try:
                xml = adapter._get_entries_for_page(nbid, tree_id)
                entries = adapter._parse_entries_xml(xml)
            except Exception as exc:
                print(f"[la_crawl] Skipping page {display!r}: {exc}")
                fetch_failed.add(_safe(display))
                continue
            # Tier 1: record this page's embedded images (URLs only — no fetch, no cookie),
            # keyed by the same safe-name as the la_page doc so Tier 2 can map KG hit -> figures.
            if images_out is not None:
                imgs = extract_embedded_img_urls(xml)
                if imgs:
                    images_out[_safe(display)] = {
                        "page": display, "nbid": nbid, "tree_id": tree_id, "images": imgs,
                        # LabArchives assigns each inline image a stable id, carried in
                        # its URL as file_name=<id>.png — 21 distinct ids for 21 images
                        # on a real page. Recording them means a figure keeps its
                        # identity when the page is reordered; without this the only
                        # handle is position (img_1, img_5 …), so inserting one image
                        # at the top renames every figure after it and a report's
                        # citation silently points at a different picture.
                        "image_ids": [_la_image_id(u) for u in imgs],
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
    if status_out is not None:
        status_out.update(tree_failures=len(failures), fetch_failed=fetch_failed,
                          truncated=truncated, notebooks=len(nbs))
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
# The most stale pages one crawl may remove (or 10% of the corpus, if more).
MAX_PRUNE = 5


def prune_corpus(keep: set[str]) -> list[str]:
    """Delete crawled page files not in *keep* (safe names); return the names removed.

    Only called after a complete crawl. write_corpus never removed anything, so a renamed
    page stayed in the corpus under its old name (20261014_Qubit.md beside its new
    20261014_BE260213_2_D2.md, same tree_id), and a deleted or emptied page stayed in the
    graph forever: the refresh's "removed" never fired.
    """
    la_dir = KNOWLEDGE_ROOT / "labarchives"
    removed = []
    for f in la_dir.glob("*.md"):
        if f.stem not in keep:
            try:
                f.unlink()
                removed.append(f.stem)
            except OSError as exc:
                print(f"[la_crawl] could not remove stale page {f.name}: {exc}")
    return sorted(removed)


def _load_image_manifest() -> dict:
    try:
        return json.loads(IMAGE_MANIFEST_PATH.read_text(encoding="utf-8")).get("pages", {})
    except Exception:  # noqa: BLE001 — missing or unreadable: start over
        return {}


def write_image_manifest(images_by_page: dict, complete: bool = True,
                         keep: set[str] | None = None) -> int:
    """Write the Tier-1 per-page image manifest; return total image count.

    Maps ``{safe_page_name: {page, nbid, tree_id, images:[urls]}}`` — the safe name
    matches the ``la_page:<name>`` KG doc, so once a query resolves to a page, Tier 2
    can look up its figures here and fetch them on demand (cookie-gated).

    A partial crawl (``complete=False``) updates the pages it saw and keeps the rest. It
    used to replace the file, so a 25-page smoke run left every other page with "No
    figures found". *keep* names pages whose fetch failed: their old entries are kept.
    """
    IMAGE_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    old = _load_image_manifest()
    if complete:
        pages = {k: v for k, v in old.items() if k in (keep or set())}
        pages.update(images_by_page)
    else:
        pages = dict(old)
        pages.update(images_by_page)
    total = sum(len(v.get("images", [])) for v in pages.values())
    IMAGE_MANIFEST_PATH.write_text(json.dumps({"pages": pages}, indent=2), encoding="utf-8")
    return total


def crawl_to_bundle(notebooks: list[str] | None = None, max_pages: int | None = None,
                    exclude_folders: set[str] | None = None, adapter=None) -> int:
    """Crawl LabArchives and write the corpus into knowledge/labarchives/. Returns count.

    Defaults: notebooks -> the lab_config "Primary notebook" (KG is scoped to it);
    exclude_folders -> the lab_config "Upload folder" (the AI Agent folder — LORE's own
    reports are kept out of the graph).
    """
    default_scope = notebooks is None and exclude_folders is None
    if notebooks is None:
        primary = lab_config_value("Primary notebook").strip()
        notebooks = [primary] if primary else None
    if exclude_folders is None:
        upload = lab_config_value("Upload folder").strip()
        exclude_folders = {upload} if upload else set()
    images_out: dict = {}
    status: dict = {}
    # The Slack archive's week pages sit in a subfolder of the upload folder; they are the
    # one part of it the graph indexes.
    slack = lab_config_value("Slack archive folder", "Slack").strip()
    corpus = crawl(adapter=adapter, notebooks=notebooks, max_pages=max_pages,
                   exclude_folders=exclude_folders, images_out=images_out, status_out=status,
                   include_within={slack} if slack else set())
    n = write_corpus(corpus)
    # Complete = the usual scope, uncapped, every folder listed. Only then is a page that
    # was not seen known to be gone; a partial crawl prunes nothing.
    complete = (default_scope and max_pages is None and not status.get("tree_failures")
                and not status.get("truncated") and status.get("notebooks", 0) > 0
                and len(corpus) > 0)
    fetch_failed = status.get("fetch_failed", set())
    if complete:
        keep = {_safe(d["page"]) for d in corpus} | fetch_failed
        existing = [f.stem for f in (KNOWLEDGE_ROOT / "labarchives").glob("*.md")]
        stale = [s for s in existing if s not in keep]
        # A safety valve: a crawl that suddenly sees far fewer pages is more likely broken
        # (a permission change, a renamed notebook) than a lab that deleted its notebook.
        if len(stale) > max(MAX_PRUNE, len(existing) // 10):
            print(f"[la_crawl] NOT removing {len(stale)} of {len(existing)} pages: too many "
                  "at once to trust. Check the crawl; remove them by hand if they are gone.")
        else:
            removed = prune_corpus(keep)
            if removed:
                print(f"[la_crawl] removed {len(removed)} page(s) no longer in LabArchives "
                      f"(renamed, deleted or emptied): {', '.join(removed)}")
    else:
        print("[la_crawl] partial crawl: no stale pages removed, figure manifest merged")
    n_imgs = write_image_manifest(images_out, complete=complete, keep=fetch_failed)
    print(f"[la_crawl] image manifest: {len(images_out)} page(s) crawled, {n_imgs} image(s) "
          "listed")
    return n
