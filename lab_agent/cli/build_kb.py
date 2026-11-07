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

    code = 0
    try:
        with build_lock():
            n = la_crawl.crawl_to_bundle(notebooks=notebooks, max_pages=max_pages)
            print(f"[build_kb] wrote {n} LabArchives page(s) to knowledge/labarchives/")

            if "--index" in args:
                code = _build_index(full="--full" in args)
    except BuildLockHeld as exc:
        # Not a success: the listener used to log "finished (exit 0)" and reload a graph
        # another builder was still writing.
        print(f"[build_kb] another build is already in progress — skipping this run ({exc}).")
        sys.exit(EXIT_LOCK_HELD)
    sys.exit(code)


def _gather_corpus() -> dict[str, str]:
    """Collect {doc_id: text} for the KG — LabArchives pages only.

    Deliberately excludes `knowledge/experiments/` (LORE-generated report summaries): the
    graph reflects the lab's own LabArchives notes, not LORE's reports. The one exception
    is the Slack archive's week pages (crawled from the upload folder's Slack subfolder):
    LORE writes them, but they are the only record of what the lab said in Slack.
    """
    from ..config import KNOWLEDGE_ROOT

    docs: dict[str, str] = {}
    la = KNOWLEDGE_ROOT / "labarchives"
    if la.is_dir():
        for f in sorted(la.glob("*.md")):
            docs[f"la_page:{f.stem}"] = f.read_text(encoding="utf-8", errors="replace")
    return docs


# Exit codes the listener's refresh can tell apart from a real success.
EXIT_DEPS_MISSING = 3   # --index on an interpreter without the RAG packages
EXIT_LOCK_HELD = 4      # another build holds the lock
EXIT_PARTIAL = 5        # some pages failed, or the run stopped early

# An extraction error that means every later page would fail the same way.
_STOP_ALL = ("failed to authenticate", "oauth", "re-authenticate", "usage limit",
             "credit balance", "invalid api key", "not on path")


def _build_index(full: bool = False) -> int:
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
        return EXIT_DEPS_MISSING

    docs = _gather_corpus()
    manifest = {} if full else load_manifest()
    plan = plan_refresh(docs, manifest)

    kg = KnowledgeGraph(kb_dir())
    cur = dict(manifest)                         # running manifest, checkpointed per doc
    for doc_id in plan["unchanged"]:             # upgrades legacy hashes; no LLM cost
        cur[doc_id] = plan["next_manifest"][doc_id]
    to_index = list(plan["new"])
    failed: list[str] = []
    stopped = None
    try:
        for doc_id in plan["changed"] + plan["removed"]:
            # Drop the old version first. If that fails, keep the doc's manifest entry so it
            # is tried again: re-inserting an id LightRAG still holds is silently dropped.
            if not kg.delete(doc_id):
                failed.append(doc_id)
                continue
            if doc_id in plan["removed"]:
                cur.pop(doc_id, None)
            else:
                to_index.append(doc_id)
        save_manifest(cur)
        for i, doc_id in enumerate(to_index, 1):
            try:
                kg.insert(doc_id, docs[doc_id])  # (re-)index — the only token-costing step
            except Exception as exc:             # noqa: BLE001 — one bad page shouldn't stop the build
                print(f"[build_kb]   FAILED {i}/{len(to_index)} {doc_id}: {exc}", flush=True)
                failed.append(doc_id)            # not marked done → retried on the next run
                if any(k in str(exc).lower() for k in _STOP_ALL):
                    stopped = str(exc)[:200]
                    break
                continue
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
    if stopped:
        # Each later page would fail the same way, and while it failed LightRAG re-tried
        # every earlier failure on every insert: 36 failed extractions on 2026-10-22.
        print(f"[build_kb] STOPPED early: {stopped}. The Claude Code login used for the "
              "graph needs attention (`claude` in a terminal on this machine).")
    if failed:
        print(f"[build_kb] {len(failed)} page(s) not indexed, retried next run: "
              f"{', '.join(failed[:10])}{' …' if len(failed) > 10 else ''}")
    if reindexed or plan["removed"]:
        try:
            from ..rag.service import trigger_reload
            if trigger_reload():
                print("[build_kb] told the warm KG service to reload.", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[build_kb] reload signal failed: {exc}", flush=True)
    return EXIT_PARTIAL if failed or stopped else 0


if __name__ == "__main__":
    main()
