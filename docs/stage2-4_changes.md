# Stages 2–4 Changes — Completeness, Quality Gates, Cross-Run Memory

In-depth summary of the final roadmap stages: **Stage 2** (ingestion completeness),
**Stage 3** (output quality gates), **Stage 4** (cross-run knowledge & Q&A payoff).
Companion to `stage0_changes.md` and `stage1_changes.md`. Stage 5 (LightRAG) remains
the deliberately-deferred future upgrade over the same knowledge bundle.

Date: 2026-07-30. Branch: `main` (uncommitted). All changes behind the eval harness;
**55 test functions across 10 files pass**, 35 modules import clean.

---

## Stage 2 — Ingestion completeness  *(goal #1: "all content")*

Stage 1 deepened the content types already handled; Stage 2 closes the holes where
whole content types were opaque or dropped.

| Change | Old system | New system |
|--------|-----------|-----------|
| **Standalone `.py` scripts** | Fetched (classified `code`) but written to no output file — invisible to analysts | Written to `scripts.md`; also AST-scanned for lab dependencies |
| **Binary data files** | `.npy/.npz/.h5/…` noted only as "exists" | Summarized into `data_summaries.md`: `.npy` shape/dtype, `.npz` members, HDF5 dataset names/shapes (guarded imports) |
| **Multi-repo experiments** | Only the *first* auto-discovered GitHub URL fetched | Additional discovered repos fetched too (capped at 3, best-effort) |
| **Tree truncation** | Printed a warning, then forgotten | Surfaced in `metadata.json` (`github_tree_truncated`) + the report notes it (Stage 1 WS2) |

**Logic.** `collect/binary.py` introspects data binaries straight from the tarball
snapshot (already in memory — no extra fetch), fully guarded so a missing `numpy`/`h5py`
or a corrupt file degrades to a size note. `run_pipeline` writes `scripts.md` and folds
binary summaries into `data_summaries.md`. The dependency import scan now covers `.py`
files in addition to notebooks.

**Deferred (documented):** listing *non-image* LabArchives attachments (PDFs/data) is
network-gated and low-value (attachments are almost always images, which we already
handle) — left as a known edge.

**Files.** `collect/binary.py` (new), `collect/dependencies.py`, `sources/github.py`,
`cli/run_pipeline.py`, `collect/okf.py`. **Tests.** `tests/test_ingestion_completeness.py`.

---

## Stage 3 — Output quality & accuracy gates  *(goal #3)*

| Change | Old system | New system |
|--------|-----------|-----------|
| **Link verification (WS4)** | Reports could cite dead/wrong links unchecked | `verify_links.py` checks every URL in code (GitHub via API+token, LabArchives auth-walled, others HEAD/GET) → `link_check.md`; report writes `[MISSING: <url>]` for dead links; critic item 8 enforces it |
| **Critic vocabulary (WS3)** | Binary `PASS`/`FAIL`; a gap silently forced a revision | Four outcomes: `passed` / `gaps_found` / `expert_needed` / `human_needed` — "needs an expert" is now a first-class escalation that stops and asks the user instead of uploading a wrong report or looping |
| **Citations (OKF Phase 2)** | Only a `Source` column | Formal `# Citations` section; every Key Parameters row carries a resolving `[n]`; critic item 9 checks citation structure |

**Logic.** `verify_links` is deterministic (code decides, not the model) and never
false-fails auth-walled LabArchives links. The real-run smoke actually caught a bug in
*itself* — URLs hugged by trailing backticks/`**` in prose were sent with the junk
attached and 404'd; fixed by trimming trailing markdown punctuation. The critic's new
outcomes reuse the existing "ask and exit" delivery pattern (Step 0/2b), so escalation
needs no new machinery; the orchestration rules in `CLAUDE.md` route each outcome.

**Files.** `cli/verify_links.py` (new), `.claude/agents/critic.md`, `CLAUDE.md`,
`docs/report_style_guide.md`. **Tests.** `tests/test_verify_links.py` + a live smoke.

---

## Stage 4 — Cross-run knowledge & the Q&A payoff  *(goal #4)*

The Stage-0 Q&A fast-path searched per-run `outputs/`. Stage 4 adds a curated,
cross-run **knowledge bundle** — the durable answer to "what did we do before?"

| Change | Old system | New system |
|--------|-----------|-----------|
| **Knowledge bundle (OKF Phase 3)** | No cross-run memory; lookups leaned on lossy Slack search | `record_knowledge.py` distils each run into `knowledge/experiments/<id>.md` (objective, key parameters, outcome, commit provenance) + `log.md` + `index.md` after upload |
| **Q&A search** | `outputs/` only | Also searches the curated `knowledge/` concepts; resolution order is knowledge/archive → Slack → LabArchives → ask user |
| **Feedback from signing** | Human edits lost | `--sign` promotes a concept to a high-trust `status: signed` exemplar |

**Logic.** The bundle is plain markdown + OKF frontmatter — greppable, git-diffable,
and it survives cookie expiry. It is a **finding aid only**: retrieval resolves *what to
look at* and cites experiment IDs; it is never a source of new report numbers (the trust
invariant). It is also the storage spine a later LightRAG layer (Stage 5) would index
over — no rework needed when the archive outgrows grep.

**Verified on real data:** recording the real `power_calibration_20260227` run produced a
correct Experiment concept (5-bullet objective, `ac7ee2a` commit provenance), and the
Q&A search picks it up.

**Feedback-loop scope (honest):** the lightweight version marks a report as a signed,
high-trust exemplar. The fuller version — diffing the AI draft against the human-edited
version pulled back from LabArchives — is network/cookie-gated and left as future work.

**Files.** `cli/record_knowledge.py` (new), `cli/ask.py`, `config.py`, `CLAUDE.md`,
`.gitignore`. **Tests.** `tests/test_knowledge_bundle.py`.

---

## Testing

10 test files (55 test functions), all green; 35 modules import clean; the eval
structural `check` and golden `diff` are both clean on the real `power_calibration`
run. Two bugs were caught and fixed *by* the real run / test suite: WS6's
fractional-second timestamps (Stage 1) and `verify_links`' trailing-markdown URL
trimming (Stage 3) — plus a test-isolation fix when the knowledge bundle joined the
Q&A corpus.

## What's done vs. deferred
- **Done:** Stages 0–4 — all `#12` workstreams, OKF Phases 1–3, ingestion completeness,
  quality gates, cross-run knowledge + Q&A.
- **Deferred (by design):** Stage 5 (LightRAG retrieval over the knowledge bundle) — the
  bundle is built to be indexed when the archive outgrows grep; the signing feedback
  loop's edit-capture; and non-image LabArchives attachment listing.

See [ROADMAP.md](../ROADMAP.md) for the full picture.
