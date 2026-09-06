# Stage 1 Changes — Extraction Depth

In-depth summary of Stage 1 from the [roadmap](../ROADMAP.md): the `#12` accuracy
workstreams (WS1, WS2, WS5, WS6) plus OKF Phase 1 frontmatter. Where Stage 0 built
safety nets, **Stage 1 changes what the pipeline actually extracts** — the goal being
"stop the agents from guessing, give them the ground truth."

Date: 2026-07-30. Branch: `main` (uncommitted). All changes run behind the Stage 0
eval harness. 43 fixture tests pass across 7 files; the whole package imports clean.

---

## At a glance

| WS | Change | Goal | Old system | New system |
|----|--------|------|-----------|-----------|
| **1** | Notebook code + cell outputs (incl. plot images) | #1 ingest | `notebooks.md` = **markdown cells only**; code and result plots never reached the analysts | Full code + outputs (stream/result/error) + **extracted `image/png` plots** |
| **2** | Provenance + timestamps | #1, freshness | Artifacts anonymous; `metadata.json` had 3 keys | Commit SHA/date, LA entry timestamps, enriched `metadata.json`, self-describing OKF files |
| **5** | AST dependency extraction | #1, Methods | Line-regex imports; source `[:200]`-truncated | AST imports; **symbol-targeted** source (constants + imported symbols) |
| **6** | CSV summarization | #1 data | min/max only | Per-column stats, value samples, timestamp ranges, sample rows |
| OKF P1 | Frontmatter + `index.md` | storage | Plain files, filename-convention handoffs | Typed, self-describing concepts + a browsable index |

---

## WS1 — Notebook code + cell-output plots  *(fixes the "plots in notebooks" bug)*

**The problem.** `notebooks.md` was written from `summary.notebook_markdown` — **markdown
cells only**. The full code lived in memory but was never written to disk, and code-cell
**outputs were never captured at all**. So the GitHub Analyst described "what the code
does" without seeing the code, and any result rendered as a plot *inside a notebook*
(the answer, in many runs) was invisible to the model. This was the reported
"agent doesn't understand the plots in notebooks" bug.

**The logic now.** New `serialize_notebook(text, image_prefix)` in `collect/ingest.py`
emits, per cell:
- The cell source under a typed header; code headers include `execution_count`
  (or `unexecuted`) so an out-of-order/unrun notebook is visible.
- An `### Output` block for code cells: `stream` text, `execute_result`/`display_data`
  `text/plain`, and `error` (`ename: evalue` + last ~10 traceback lines, **ANSI-stripped**).
- **`image/png` outputs decoded** and returned as `(filename, bytes)`, with a
  `[output image: github_images/<name>]` marker in the text.

`github.py._collect` now returns a *list* — the notebook artifact **plus one `figure`
artifact per output image** (bare filename → saved into `github_images/` by the existing
image path, matching the marker). `load()` flattens; the fragile `.count("[code]")`
primary-notebook re-rank was replaced with a robust regex on the serialized content.
`run_pipeline` writes `notebooks.md` from the full serialized content. A 4000-char
per-output cap keeps logging-heavy sweeps from bloating the file.

**Improvement.** The analysts now see the real code *and the real result plots*.
Fewer procedure hallucinations, and the "answer is in the plot" case works.

**Files.** `collect/ingest.py`, `sources/github.py`, `cli/run_pipeline.py`.
**Tests.** `tests/test_notebook_serialize.py` (7, incl. a no-network `_collect` integration test).

---

## WS2 — Provenance + timestamps

**The problem.** Collected artifacts carried no record of *where or when* they came from,
and `metadata.json` held only `experiment_id`/`la_pages`/`github_url`. Reports couldn't
pin a value to a commit, correlate steps with time, or flag stale/truncated inputs.

**The logic now.**
- `CollectedArtifact` gains `source_ref` / `created_at` / `updated_at`.
- The GitHub adapter resolves the ref to a concrete **commit SHA + date** (one API call,
  best-effort) and stamps every artifact; it also records the **tree-truncated** flag.
- The LabArchives adapter reads **entry id + created/updated timestamps** (guarded tag
  lookup — several tag-name variants tried, `None` when absent). *This is the one part
  gated on a live-credential check of the exact XML tag names; it is implemented
  defensively so it never breaks parsing (marked with a TODO).*
- `metadata.json` now includes `github_commit_sha/date`, `github_tree_truncated`,
  `la_entry_count`, **all** `discovered_github_urls` (previously the extras were dropped),
  `run_timestamp`, and a `files` inventory.
- The Report Writer style guide now pins GitHub links to the commit SHA and notes tree
  truncation when present.

**Improvement.** Claims become traceable to an exact snapshot; freshness and
missing-file risks are visible. This provenance is also the seed of the future
cross-run knowledge bundle (Stage 4).

**Files.** `models.py`, `sources/github.py`, `sources/labarchives/adapter.py`,
`cli/run_pipeline.py`, `docs/report_style_guide.md`.
**Tests.** `tests/test_provenance_okf.py` (5).

---

## OKF Phase 1 — self-describing knowledge bundle

Folded in with WS2. New `collect/okf.py` gives every collection output file a small YAML
frontmatter block (`type`, `resource`, `source_ref`, `timestamp`, `tags`) and writes a
reserved `index.md`. Frontmatter is kept **deterministic** (source-derived timestamps
only, never the run time) so the eval golden-diff stays stable; the run timestamp lives
only in `metadata.json` (added to the diff's volatile-key ignore-list). `finalize_bundle`
is idempotent. The run directory is now a browsable, machine-readable OKF bundle — the
storage spine the knowledge bundle and later LightRAG (Stages 4–5) will index.

**Files.** `collect/okf.py`, `cli/run_pipeline.py`, `cli/eval.py`.
**Tests.** in `tests/test_provenance_okf.py`.

---

## WS5 — AST dependency extraction

**The problem.** Imports were scanned with a line regex (fragile on multiline/aliased
imports, and — after WS1 — at risk of reading imports out of captured *output* text), and
each dependency source file was blindly truncated to its first 200 lines, so the exact
hardware constants and the imported symbols' real code were hit-or-miss.

**The logic now.**
- Code-cell sources are recovered from the serialized notebook content
  (`_code_blocks_from_serialized`) so import scanning sees code only, never output text.
- Imports are parsed with `ast` (handles multiline/parenthesized imports and aliases;
  IPython magics stripped; relative imports skipped), with the line regex as a fallback.
- Dependency source is rendered **symbol-targeted** (`_render_symbols`): the module
  docstring, **every top-level constant** (e.g. `DAC_CURRENT = 40_500`), and the **full
  source of the specifically imported symbols**; everything else is a one-line stub list.
  Unparseable files fall back to the first-200-line slice.

**Improvement.** The Dependencies Analyst reliably gets the actual constants and the code
of what the notebook actually imported, instead of a truncation lottery — better Methods
sections and reliable "notebook sets X vs package default Y" cross-references.

**Files.** `collect/dependencies.py`. **Tests.** `tests/test_deps_ast.py` (5).

---

## WS6 — CSV summarization

**The problem.** Large CSVs were summarised as bare `col: [min, max]` — no distributions,
no sense of format, and columns with no numbers were reported as "no numeric columns".

**The logic now.** `_summarize_large_csv` emits, per file: row/column counts and header;
per numeric column **min/max/mean/median/count**; mixed columns split numeric vs
non-numeric; **non-numeric columns sampled** (up to 8 distinct values — catches
channel/status columns); **timestamp columns detected** with first/last; the **first 3 and
last 3 rows verbatim** as a mini table; and a note that full data isn't included so per-row
claims are unverifiable (feeds the accuracy rules).

**Improvement.** Analysts see real distributions, units/format, and time ranges (usable for
DR correlation) instead of a bare range.

**Files.** `collect/summarize.py`. **Tests.** `tests/test_csv_summary.py` (5).

---

## Testing

```bash
# 43 tests across 7 files, all green:
for t in tests/test_slack_unwrap tests/test_eval_harness tests/test_ask_search \
         tests/test_notebook_serialize tests/test_provenance_okf \
         tests/test_deps_ast tests/test_csv_summary; do python "$t.py"; done
```
Whole `lab_agent` package (31 modules) imports with no errors; the eval structural
`check` stays clean on a real report.

---

## Important: re-baseline the golden after the next real run

Stage 1 **intentionally** changes deterministic outputs (`notebooks.md` now has code +
outputs; `metadata.json` has new keys; four files gained OKF frontmatter). The Stage 0
golden baseline is therefore intentionally stale. On the next *real* pipeline run,
re-baseline with:

```bash
python -m lab_agent.cli.eval diff outputs/<experiment_id> --update
```

## Known minor limitations (by design)
- LabArchives entry timestamp tag names are best-effort until confirmed against one live
  `get_entries_for_page` dump (guarded; never breaks parsing).
- Two notebooks with the *same basename* in one folder could collide on cell-image
  filenames (rare; last-write-wins).
- OKF `index.md` written at collection time lists collection files; the orchestrator can
  regenerate it after the agent phases to include `extracted_*.md`.

## What's next
Stage 2 (ingestion completeness: binary/HDF5, standalone `.py`, multi-repo, tree
truncation, LA attachments), then Stage 3 (WS4 link verify, WS3 critic vocabulary, OKF
Phase 2 citations). See [ROADMAP.md](../ROADMAP.md).
