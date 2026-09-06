"""Incremental knowledge-graph refresh — index only what changed (Stage 5, cost control).

The expensive part of a KG build is the LLM entity/relation extraction per document;
embeddings are local and crawling is free. So we keep a manifest fingerprinting each
indexed doc by a **normalized** content hash (whitespace/case/punctuation-insensitive, so
pure formatting edits don't count) and only re-index **new** or **content-changed** docs
on refresh. Removed docs are dropped from the graph.

The manifest (``knowledge/kb_manifest.json``) is the graph's state; the corpus files under
``knowledge/`` are written in full every crawl (grep stays current for free).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..config import kb_dir

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _manifest_path() -> Path:
    """Manifest lives inside the KB storage dir so it moves with it (out of OneDrive)."""
    return kb_dir() / "kb_manifest.json"


def normalized_hash(text: str) -> str:
    """Hash of content normalized to ignore case/whitespace/punctuation-only edits."""
    norm = _NON_ALNUM.sub(" ", text.lower()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def load_manifest(path: Path | None = None) -> dict[str, str]:
    path = path or _manifest_path()
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("docs", {})
    except Exception:
        return {}


def save_manifest(docs: dict[str, str], path: Path | None = None) -> None:
    path = path or _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"docs": docs}, indent=2), encoding="utf-8")


def plan_refresh(docs: dict[str, str], manifest: dict[str, str]) -> dict:
    """Classify each doc against the manifest.

    *docs* maps doc_id -> current text; *manifest* maps doc_id -> last-indexed hash.
    Returns {new, changed, unchanged, removed, next_manifest}. Only new+changed need
    the (token-costing) re-index; removed should be deleted from the graph.
    """
    new: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    next_manifest: dict[str, str] = {}
    for doc_id, text in docs.items():
        h = normalized_hash(text)
        next_manifest[doc_id] = h
        if doc_id not in manifest:
            new.append(doc_id)
        elif manifest[doc_id] != h:
            changed.append(doc_id)
        else:
            unchanged.append(doc_id)
    removed = [doc_id for doc_id in manifest if doc_id not in docs]
    return {
        "new": new, "changed": changed, "unchanged": unchanged,
        "removed": removed, "next_manifest": next_manifest,
    }
