# LORE Roadmap

Single source of truth for planned work. Consolidates and supersedes the plans in
GitHub issues [#10 (OKF)](../../issues/10), [#11 (RAG)](../../issues/11), and
[#12 (accuracy & freshness)](../../issues/12), and folds in gaps identified against
the project goal. Last updated: 2026-07-24.

---

## The goal (the measuring stick)

LORE is an effective system that:

1. **Ingests all content** of an experiment — completeness
2. **Efficiently and in a timely manner** — latency
3. **Outputs results to LabArchives** — the report
4. **Answers other lab / experiment / related questions on Slack** — Q&A

Every item below is justified by which of these four it serves. The core trust
invariants stay fixed: stateless sessions, pull-based live fetch, correctness-first,
and reports stay `[UNSIGNED]` until a human signs. Any derived knowledge layer
(bundle / RAG) is for *finding* things, never a source of report content.

---

## Status: what's actually built vs. the plans (audit, 2026-07-24)

The 14 commits pulled on 2026-07-24 (PRs #13 + #14) were **not** the #10/#11/#12
plans. They were a **refactor + performance/robustness pass** that is mostly
orthogonal to those plans but makes them easier to build.

**What Fable actually did:**
- **#13 repo-structure-review** — reorganized `lab_agent/` into `sources/ collect/
  dr/ publish/ slack/ cli/`; decomposed the monolithic CLAUDE.md into
  `.claude/agents/*.md` + `docs/*.md`; added `config.py`, `pyproject.toml`,
  `tests/`, `vendor/`.
- **#14 infallible-wing** — parallel/tarball fetch, org-repo & page-index caches,
  wiring-diagram prefetch+cache, fail-fast cookies, session resume across Slack
  replies, DR question asked up front, Phase A model tiering, edit-in-place revision.

**Plan items status:**

| Plan | Item | Status |
|------|------|--------|
| #12 | WS1 notebook code + cell outputs (incl. plot images) | ❌ not done |
| #12 | WS2 provenance / timestamps | ❌ not done |
| #12 | WS3 critic 4-outcome vocabulary | ❌ not done (still PASS/FAIL) |
| #12 | WS4 link verification | ❌ not done |
| #12 | WS5 AST dependency extraction | ❌ not done (still line-regex + `[:200]`) |
| #12 | WS6 CSV summarization | ❌ not done (still min/max only) |
| #10 | Phase 1 frontmatter + `index.md` | ❌ not done |
| #10 | Phase 2 citations | 🟡 Source column exists (pre-pull `9f11532`); no `# Citations` |
| #10 | Phase 3 knowledge bundle | ❌ not done — **but** 3.4 wiring cache done incidentally |
| #10 | Phase 4 niceties | ❌ not done |
| #11 | RAG KB (all phases) | ❌ not done |

Net: two small items are effectively free (wiring cache; Source column seeded);
everything substantive remains to build.

---

## Roadmap

Sequenced so each stage delivers standalone value, respects dependencies, and every
extraction/prompt change is protected by the eval harness built in Stage 0.

### Stage 0 — Foundations & quick wins  *(goals #2, #4; de-risks all later work)*
- **Slack-link fix + link-security hardening** — unwrap Slack `<url>` / `<url|label>`
  formatting and entity-escapes in the `slack/` package (fixes GitHub links sent via
  Slack); fold in host-allowlist + hard caps *inside* the GitHub auto-discovery tool.
- **Observability** — one timing + token line per phase.
- **Lightweight eval harness** — golden smoke test vs. `power_calibration_20260227`;
  diff key artifacts before/after each change.
- **Lightweight Q&A fast-path** — dedicated "answer a question" Slack mode (grep
  `outputs/` + memory + Slack/LA search), separate from the report pipeline.

### Stage 1 — Extraction depth  *(#12 core; goal #1; fixes the notebook-plots bug)*
- **WS1** notebook code + cell **outputs incl. plot images** → fixes plots bug.
- **WS2** provenance / timestamps → also seeds the knowledge bundle + `metadata.json`.
  *(Gate: verify LabArchives timestamp tag names empirically first — needs `.env`.)*
- **WS5** AST dependency extraction (imported symbols; drop `[:200]` truncation).
- **WS6** richer CSV summaries (per-column stats, sample rows, timestamp detection).
- **OKF Phase 1** frontmatter + `index.md` — folded into the WS1/WS2 output writers.

### Stage 2 — Ingestion completeness  *(goal #1; remaining "all content" holes)*
- Binary / HDF5 / `.npy` summarization (shapes, dataset names, ranges).
- Standalone `.py` analysis scripts (not just notebooks).
- Multi-repo experiments (fetch all discovered GitHub URLs — WS2 captures them).
- Git tree-truncation handling (surface + mitigate).
- LabArchives attachments beyond images (linked PDFs / data files).

### Stage 3 — Output quality & accuracy gates  *(#12 tail + OKF P2; goal #3)*
- **WS4** `verify_links.py` + critic item.
- **WS3** critic 4-outcome vocabulary (`passed` / `gaps_found` / `expert_needed` /
  `human_needed`) + escalation UX (reuses the Step-2b "ask and exit" pattern).
- **OKF Phase 2** formal `# Citations` section, extending the Source column.

### Stage 4 — Cross-run knowledge & the Q&A payoff  *(goal #4, durable version)*
- **OKF Phase 3** knowledge bundle (`experiments/ devices/ fridge/ references/`,
  `log.md`, `index.md`, resolution-order wiring) — upgrades the Stage-0 Q&A fast-path
  from grep to structured lookup.
  *(Gate: where `knowledge/` lives; backfill vs. forward-only; commit `outputs/`?)*
- **Feedback loop from signing** — capture human edits on `[UNSIGNED]`→signed and
  feed them back as high-trust exemplars.

### Stage 5 — Scale-up  *(deferred upgrade path)*
- **#11 LightRAG** over the same knowledge bundle, only when it outgrows grep.
  *(Gate: go/no-go on archive size; embedding backend Voyage vs. local; `kb/` location.)*

---

## Open questions (gating specific stages only)

- **Stage 1 / WS2:** confirm LabArchives entry timestamp tag names (dump one raw
  `get_entries_for_page` response) before coding against them.
- **Stage 4 / OKF P3:** where does `knowledge/` live (gitignored here vs. separate
  private repo)? backfill from past reports or grow forward-only? do `outputs/`
  bundles get committed for history?
- **Stage 5 / #11:** is the archive big enough to justify RAG? embedding backend?
  `kb/` local vs. shared?

Everything else can proceed without further decisions.

---

## Consistency with the goal (guardrails)

- Two tensions to manage, both already guardrailed: WS1 enlarges `notebooks.md`
  (depth vs. "timely" — bounded by per-output truncation caps + the LabArchives
  640 KB image budget); #11 retrieval is fuzzy (retrieval-for-finding-only, never
  report content).
- The eval harness precedes every extraction/prompt change — the highest-leverage
  regression guard given how much of LORE lives in editable prompts.
- Work lands on `main` via feature branches; `conference-api-version` stays frozen.
