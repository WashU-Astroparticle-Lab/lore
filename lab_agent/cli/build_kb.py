"""Build / refresh the local knowledge corpus (Stage 5).

v1 crawls LabArchives page *text* into ``knowledge/labarchives/`` — the corpus the Q&A
search covers (grep now; the LightRAG knowledge-graph engine in ``lab_agent.rag`` later).
LabArchives text is HMAC-authed, so this needs no session cookies. Images are v2.

Usage:
  python -m lab_agent.cli.build_kb                      # crawl all notebooks
  python -m lab_agent.cli.build_kb --notebook "Qubit & KID"
  python -m lab_agent.cli.build_kb --max-pages 25       # cap (e.g. a smoke run)
"""
from __future__ import annotations

import sys

from ..collect import la_crawl
from ..config import load_env


def main() -> None:
    load_env()
    args = sys.argv[1:]
    notebooks: list[str] | None = None
    max_pages: int | None = None
    if "--notebook" in args:
        notebooks = [args[args.index("--notebook") + 1]]
    if "--max-pages" in args:
        try:
            max_pages = int(args[args.index("--max-pages") + 1])
        except (IndexError, ValueError):
            print("--max-pages requires an integer")
            sys.exit(2)

    # Serialize the whole run (crawl + index) behind the cross-process build
    # lock. The Slack listener's nightly thread and the Task Scheduler fallback
    # both land here; the lock guarantees they never crawl/index concurrently
    # (which would corrupt the corpus or the LightRAG graph). A second builder
    # that finds the lock held skips this run rather than racing.
    from ..rag.build_lock import BuildLockHeld, build_lock

    try:
        with build_lock():
            n = la_crawl.crawl_to_bundle(notebooks=notebooks, max_pages=max_pages)
            print(f"[build_kb] wrote {n} LabArchives page(s) to knowledge/labarchives/")

            if "--index" in args:
                _build_index(full="--full" in args)
    except BuildLockHeld as exc:
        print(f"[build_kb] another build is already in progress — skipping this run ({exc}).")
        sys.exit(0)


def _gather_corpus() -> dict[str, str]:
    """Collect {doc_id: text} for the KG — LabArchives pages only.

    Deliberately excludes `knowledge/experiments/` (LORE-generated report summaries):
    the graph reflects the lab's own LabArchives notes only, not LORE's output.
    """
    from ..config import KNOWLEDGE_ROOT

    docs: dict[str, str] = {}
    la = KNOWLEDGE_ROOT / "labarchives"
    if la.is_dir():
        for f in sorted(la.glob("*.md")):
            docs[f"la_page:{f.stem}"] = f.read_text(encoding="utf-8", errors="replace")
    return docs


def _build_index(full: bool = False) -> None:
    """Incrementally refresh the LightRAG KG — only new/changed docs are (re-)indexed.

    The manifest fingerprints each indexed doc so unchanged pages cost no LLM tokens.

    ``--full`` **ignores the manifest; it is not a from-scratch rebuild.** The existing
    vector stores stay on disk, so it cannot be used to change the embedding model: the
    vdb files keep the old dimension and every insert dies with "Embedding dim mismatch".
    It also writes an empty manifest *before* inserting, so an aborted ``--full`` leaves
    a populated graph with no manifest and the next refresh re-extracts everything.
    To swap embedding models, start from a clean KB directory —
    see ``docs/embedding_model_swap.md``.
    """
    from ..config import kb_dir
    from ..rag import KnowledgeGraph, backend_available
    from ..rag.incremental import load_manifest, plan_refresh, save_manifest

    if not backend_available():
        print("[build_kb] RAG deps not installed — skipping index. "
              "Enable with:  pip install -e '.[rag]'")
        return

    docs = _gather_corpus()
    manifest = {} if full else load_manifest()
    plan = plan_refresh(docs, manifest)

    kg = KnowledgeGraph(kb_dir())
    cur = dict(manifest)                         # running manifest, checkpointed per doc
    to_index = plan["new"] + plan["changed"]
    try:
        for doc_id in plan["changed"] + plan["removed"]:
            kg.delete(doc_id)                    # drop the old version first
        for doc_id in plan["removed"]:
            cur.pop(doc_id, None)
        save_manifest(cur)
        for i, doc_id in enumerate(to_index, 1):
            try:
                kg.insert(doc_id, docs[doc_id])  # (re-)index — the only token-costing step
            except Exception as exc:             # noqa: BLE001 — one bad page shouldn't stop the build
                print(f"[build_kb]   FAILED {i}/{len(to_index)} {doc_id}: {exc}", flush=True)
                continue                         # not marked done → retried on the next run
            cur[doc_id] = plan["next_manifest"][doc_id]
            save_manifest(cur)                   # checkpoint → interrupted builds resume here
            print(f"[build_kb]   indexed {i}/{len(to_index)}: {doc_id}", flush=True)
    finally:
        kg.close()

    reindexed = len(plan["new"]) + len(plan["changed"])
    print(f"[build_kb] KG refresh: {len(plan['new'])} new, {len(plan['changed'])} changed, "
          f"{len(plan['unchanged'])} unchanged (skipped), {len(plan['removed'])} removed. "
          f"Re-indexed {reindexed}/{len(docs)} docs — no LLM cost on the "
          f"{len(plan['unchanged'])} unchanged.")


if __name__ == "__main__":
    main()
