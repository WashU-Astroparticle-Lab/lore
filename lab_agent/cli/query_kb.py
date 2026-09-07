"""Query the cross-run knowledge graph (Stage 5c), with a keyword-search fallback.

Uses the LightRAG KG under ``knowledge/kb/`` when the optional deps are installed and the
index is built; otherwise falls back to the grep search over the knowledge bundle +
outputs (``lab_agent.cli.ask``), so this command always works.

Usage:
  python -m lab_agent.cli.query_kb "has this resonator's f0 drifted since March?"
  python -m lab_agent.cli.query_kb "..." --mode local|global|hybrid
"""
from __future__ import annotations

import sys

from ..config import kb_dir, load_env
from ..rag import KnowledgeGraph
from . import ask


def main() -> None:
    load_env()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = sys.argv[1:]
    mode = "hybrid"
    if "--mode" in args:
        i = args.index("--mode")
        mode = args[i + 1] if i + 1 < len(args) else "hybrid"
        del args[i:i + 2]
    question = " ".join(args).strip()
    if not question:
        print(__doc__)
        sys.exit(2)

    kg = KnowledgeGraph(kb_dir())
    graph_ok = kg.available() and kg.is_built()
    if graph_ok:
        # Try the warm resident service first (listener-hosted) — it skips the ~25-40s
        # model/graph cold start. If it isn't up (dev, or still warming), fall back to
        # loading the graph in-process, exactly as before.
        from ..rag import service as kg_service

        answer = kg_service.query_via_service(question, mode)
        if answer is None:
            try:
                answer = kg.query(question, mode=mode)
            finally:
                kg.close()
        print(answer)

    # Hybrid resolution. The graph is strong on concepts but weak on raw identifiers:
    # the LLM entity extraction may never turn a chip/run ID or a filename buried in a
    # hyperlink (e.g. "BE260416") into an entity, so a graph-only query can't map that ID
    # to its page. Keyword/TF-IDF search nails exact identifiers. So we ALWAYS also run a
    # keyword pass and surface the top page matches as resolution hints — that lets the
    # agent recover the page the graph couldn't name (then fetch its figures, etc.). Free:
    # keyword search costs no LLM tokens.
    if graph_ok:
        # Hybrid resolution candidates (identifier matches first, then deep keyword) — the
        # same list the QA-resolution eval checks. Lets the agent recover a page the graph
        # answer couldn't name (e.g. a bare chip/run ID) and proceed.
        cands = ask.candidate_pages(question, 6)
        if cands:
            print("\n--- Candidate pages (identifier / keyword match) — use these to resolve an "
                  "ID the graph answer did not name (e.g. a chip/run ID); for a figure or number "
                  "question, run fetch_page_images on the best match instead of asking the user ---")
            for src, tag in cands:
                print(f"  [{src}]  ({tag})")
        return

    # Say WHY the graph is unavailable, loudly and first. These are two different
    # failures with different fixes, and collapsing them into one quiet
    # parenthetical hid a real one: a session running under the wrong interpreter
    # got keyword-only results, reported "I don't have enough information", and
    # nothing indicated the graph had never been consulted.
    import sys as _sys

    deps_missing = not kg.available()
    print("=" * 72)
    if deps_missing:
        print("!! KNOWLEDGE GRAPH UNAVAILABLE — answering from keyword search only.")
        print(f"   The RAG packages are not importable by this interpreter:")
        print(f"     {_sys.executable}")
        print("   They are almost certainly installed in the project interpreter — check")
        print("   the PYTHON row in lab_config.md and re-run with that. Only if it really")
        print("   is a fresh environment: pip install -e '.[rag]'")
    else:
        print("!! KNOWLEDGE GRAPH NOT BUILT — answering from keyword search only.")
        print("   Run:  build_kb --index")
    print("   Treat what follows as a keyword match, NOT as the graph's answer, and say")
    print("   so if you report it — 'nothing found' here does not mean nothing exists.")
    print("=" * 72 + "\n")
    hits = ask.search(question, 6)
    if not hits:
        print("No matches found.")
        return
    for score, src, fname, snippet in hits:
        print(f"[{src}] {fname}  (score {score})")
        for line in snippet:
            print(f"    {line[:200]}")
        print()


if __name__ == "__main__":
    main()
