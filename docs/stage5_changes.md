# Stage 5 Changes — RAG / Knowledge Graph over LabArchives

Answers the need for fast resolution of **ambiguous requests** and easier **navigation of
the whole LabArchives corpus** (not just experiments LORE has reported on). Companion to
the earlier stage docs.

Date: 2026-07-30. Branch: `main` (uncommitted). 12 test files / 64 test functions pass;
40 modules import clean.

**Design decisions (yours):** local sentence-transformer embeddings (nothing leaves the
lab), **build LLM = the Claude Code plan via headless `claude -p` (no external API, no
Ollama)**, v1 corpus = LabArchives text + our knowledge bundle, index lives local
per-machine.

**BUILT + VALIDATED (2026-07-30) on real data.** `pip install '.[rag]'` (lightrag-hku
1.5.4 + sentence-transformers + torch, all local); indexed the crawled corpus into a real
graph — **102 entities / 102 relationships** in `knowledge/kb/`; queries answer complex
cross-page questions with citations (e.g. the full QUALIPHIDE cyberinfrastructure options,
people, and Strax usage); a second `build_kb --index` reported **0 re-indexed / 4 skipped
— zero LLM cost** on unchanged docs.

**The trust invariant (unchanged):** retrieval is a *finding* aid — it resolves *what to
fetch* and cites its sources; it **never** supplies report numbers. The live pull still
produces every value, so a stale index can never corrupt a report.

---

## Two halves: corpus (built now) + engine (scaffolded)

A RAG system is a **corpus** to search and a **retrieval engine** over it. The corpus half
is fully built, tested, and live-validated here; the engine half (LightRAG's knowledge
graph) is scaffolded against a clean interface and activates with one install + a build on
the lab machine — I did not ship untested glue against an external library.

### Corpus — `build_kb` / `la_crawl` *(done, live-validated)*

| | Old | New |
|---|-----|-----|
| LabArchives coverage | Only pages LORE reported on | **Every page**, crawled into `knowledge/labarchives/` as OKF `Lab Page` concepts (HMAC auth — no cookies) |
| Q&A corpus | `outputs/` + experiment concepts | Also the crawled LA pages |
| Ranking | Raw term counts | **TF-IDF** — a rare distinctive term ("strax") beats a common one ("run") |

`lab_agent/collect/la_crawl.py` walks each notebook's page tree and extracts text; a live
3-page crawl pulled real pages across all three notebooks. The IDF upgrade is
demonstrable: the query *"strax storage RIS"* now ranks the Cyberinfrastructure LA page
**#1 (39.8 vs 9.6)** — a page never reported on — where before it was drowned out by
common-word matches in reports. That is the ambiguous-request resolution you wanted,
working today over the full corpus.

**Files.** `collect/la_crawl.py`, `cli/build_kb.py`, `cli/ask.py` (IDF + LA corpus).
**Tests.** `tests/test_la_crawl.py` + `tests/test_ask_search.py`.

### Engine — `lab_agent/rag/` + `query_kb` *(scaffolded; validate on install)*

`rag/graph.py` wraps **LightRAG** (a knowledge graph *and* a vector index in one) with:
- **Local** sentence-transformer embeddings (no data leaves the machine).
- A pluggable **build-LLM** for entity/relation extraction — Claude via `ANTHROPIC_API_KEY`,
  or swap in a local Ollama model (the embeddings are already local either way).
- Storage under `knowledge/kb/` (gitignored).

Why a graph and not just vectors: lab entities (device, cooldown, resonator, anomaly,
attenuation) recur with real relationships across pages; LightRAG's hybrid query uses
**both** the KG (multi-hop, cross-page logic) and vectors (semantic match), so we don't
have to choose.

**Isolation + graceful degradation:** all version-sensitive LightRAG calls live in one
`_LightRAGBackend` class; the rest is dependency-injected and unit-tested with a fake
backend. If the optional deps aren't installed, `query_kb` **falls back to the TF-IDF
keyword search** — so the command always works. Nothing in the pipeline depends on it.

**Everything through the Claude Code plan — no API.** The build's entity/relation
extraction LLM is the headless `claude -p` CLI (plan-authenticated; `ANTHROPIC_API_KEY`
scrubbed from the subprocess env so it uses the plan, matching how the Slack listener
spawns sessions). Prompt goes in via stdin (no argv limit); concurrency is capped
(`KB_LLM_CONCURRENCY`, default 2) since each call spawns a `claude` process. So the KG
build costs **zero pay-per-token API** — just local compute + plan usage. Embeddings are
local sentence-transformers.

**Run it:**
```bash
pip install '.[rag]'
python -m lab_agent.cli.build_kb --index            # crawl LA + build/refresh the graph
KB_BUILD_MODEL=haiku python -m lab_agent.cli.query_kb "has this resonator's f0 drifted?"
```
(`KB_BUILD_MODEL` picks the plan model for extraction, e.g. `haiku` for speed.)

**LightRAG 1.5.4 specifics handled** (in `_LightRAGBackend`): async init
(`initialize_storages` + `initialize_pipeline_status`) before use; `ainsert(ids=…)` so a
changed doc replaces rather than duplicates; `adelete_by_doc_id`; a Windows Proactor event
loop (asyncio subprocesses need it); and a `close()` that finalizes storages + cancels
LightRAG's background tasks (no shutdown noise).

**Files.** `rag/__init__.py`, `rag/graph.py`, `cli/query_kb.py`, `cli/build_kb.py`
(`--index`), `pyproject.toml` (`[rag]` extra). **Tests.** `tests/test_rag_graph.py`.

### Incremental refresh — index only what changed *(done, tested)*

The build's cost is the LLM entity-extraction per document; crawling and embeddings are
free. So a manifest (`knowledge/kb_manifest.json`) fingerprints each indexed doc by a
**normalized** content hash (case/whitespace/punctuation-insensitive, so formatting edits
don't count). On `build_kb --index`, `rag/incremental.plan_refresh` classifies every doc:

- **new** → insert; **changed** (hash differs) → delete old + re-insert; **unchanged** →
  skipped (no LLM tokens); **removed** (in manifest, gone from corpus) → deleted from graph.

So a nightly refresh only pays for genuinely new or edited pages — adding a notebook or
editing one page re-indexes just that page, not the whole corpus. Docs get a stable
`doc_id` so a changed page **replaces** its old graph entities instead of duplicating them.
`--full` forces a from-scratch rebuild. The output reports the split (e.g. "2 new, 1
changed, 143 unchanged (skipped)"). The planner + manifest are fully unit-tested; the
graph delete/replace calls are the version-sensitive backend part.

**Operational refresh:** schedule `build_kb --index` (Windows Task Scheduler / cron), e.g.
nightly. New GitHub experiments also enter the corpus automatically at report time
(`record_knowledge`) and get picked up on the next `--index`.

**Files (refresh).** `rag/incremental.py`, `cli/build_kb.py`, `rag/graph.py`
(`insert(doc_id, text)`/`delete`). **Tests.** `tests/test_incremental.py`.

---

## Status
- **Done + live-validated:** LabArchives crawler + corpus + TF-IDF search — ambiguous
  requests over the whole notebook resolve now, in the fallback keyword mode.
- **Scaffolded, needs install + one build to activate:** the LightRAG knowledge graph
  (semantic + multi-hop). The interface is tested; the LightRAG-specific glue is isolated
  and marked for validation against your installed version.
- **v2 (future):** images in the graph (vision captioning; needs cookies), incremental
  re-index on page changes, signed reports as high-trust graph exemplars, shared index.

See [ROADMAP.md](../ROADMAP.md).
