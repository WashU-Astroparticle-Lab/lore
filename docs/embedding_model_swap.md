# Swapping the knowledge-graph embedding model

Written after getting it wrong on 2026-09-17. `build_kb --index --full` is **not**
the way to do this, and the failure is not obvious from the flag's name.

## Why you would

`all-MiniLM-L6-v2` (the default) accepts **256 tokens**. LightRAG chunks up to 1200.
Measured on the live graph: 201 of 235 chunks were over the limit, a mean 56% of each
chunk's text was dropped, and the dropped tails were *not* redundant with what was
kept (median cosine 0.51 to their own head; none above 0.8). So the semantic half of
retrieval was indexing roughly the opening fifth of each chunk.

`lab_agent/rag/graph.py` now reads the window off the model and declares that to
LightRAG, so its truncation guard fires and logs instead of being silently disabled.
That makes the loss visible. It does not recover it. Recovering it needs a
long-context model, and the chunks run to 1200 tokens, so a 512-token model is not
enough either.

Candidate: `BAAI/bge-m3` — 8192-token window, 1024 dims, 568M params, ~2.2 GB, and no
`trust_remote_code`, which matters for the on-prem rule.

## Why `--full` does not work

`--full` only means "ignore the manifest, treat every doc as new". It leaves
`vdb_chunks.json`, `vdb_entities.json` and `vdb_relationships.json` on disk with the
old dimension. LightRAG loads them, then rejects every insert:

    [build_kb]   FAILED 49/187 la_page:...: Embedding dim mismatch, expected: 1024, but loaded: 384

Two further traps seen in that run:

- It calls `save_manifest({})` **before** the insert loop, so an aborted `--full`
  leaves a populated graph with an empty manifest, and the next incremental refresh
  re-extracts all 187 pages.
- Each failed insert re-initialised the backend, reloading a 568M-parameter model per
  document. It looked like progress; it was thrashing.

## The procedure that does work

1. **Stop the listener.** It holds the warm graph service and would race the rebuild.

2. **Back up the KB.** It is ~29 MB; there is no reason not to.

   ```bash
   cp -r "$LOCALAPPDATA/lore_kb" "$LOCALAPPDATA/lore_kb_backup_$(date +%Y%m%d)"
   ```

3. **Start from a clean KB directory, but keep the LLM cache.** This is the step that
   makes the rebuild cheap. `kv_store_llm_response_cache.json` holds the entity and
   relation extractions (6,976 entries / 11.3 MB at the time of writing). Carrying it
   over means extraction hits cache instead of re-calling `claude -p` once per page;
   everything else must go, because it is dimension-bound.

   ```bash
   KB="$LOCALAPPDATA/lore_kb"
   mkdir -p "$KB.new"
   cp "$KB/kv_store_llm_response_cache.json" "$KB.new/"
   mv "$KB" "$KB.old" && mv "$KB.new" "$KB"
   ```

4. **Set the model** in `.env` (not on the command line — the nightly task reads
   `.env`):

   ```
   KB_EMBEDDING_MODEL=BAAI/bge-m3
   ```

5. **Pre-download the model** before building, so a slow download is not tangled up
   with a long build:

   ```bash
   python -c "from sentence_transformers import SentenceTransformer as S; S('BAAI/bge-m3')"
   ```

6. **Build.** The manifest is absent, so every page is new and `--full` is redundant:

   ```bash
   python -m lab_agent.cli.build_kb --index
   ```

   Watch for `[kb] embeddings: ... 8192-token window` at the top and no `FAILED` lines.

7. **Verify** before restarting the listener:
   - `vdb_chunks.json` reports `embedding_dim: 1024`
   - no chunk in `kv_store_text_chunks.json` exceeds the model's window
   - `python -m lab_agent.cli.query_kb "which QPD chips have we measured and at what frequencies?"`
     returns a cross-page answer with citations

8. **Restart the listener**, which restarts the warm query service.

## Rollback

`rm -rf "$LOCALAPPDATA/lore_kb"`, copy the backup back, remove the
`KB_EMBEDDING_MODEL` line from `.env`. Nothing else holds state.

## Note for the lab machine

Do it there separately; the KB is per-machine and intentionally outside any synced
folder. The 2.2 GB model has to be downloaded there too, and `kg_nightly.bat` reads
`.env`, so once the line is set the nightly refresh picks the model up with no edit.
