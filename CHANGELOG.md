# Changelog

## v1.0 — unreleased (gated on the live test in `docs/live_test_plan.md`)

First tagged release. Everything below is on `stages-0-5-baseline`: 19 commits,
87 files, +10.8k lines over `main` (`8fa677a`, July).

Two bodies of work. Stages 0–5 (`ROADMAP.md`) built *capability*. Stages A–E
(`docs/ux_roadmap.md`) fixed *what actually reaches the human*, and came out of
reading two real Slack threads line by line rather than from a wishlist.

### Capability (Stages 0–5)

- Notebook **cell outputs**, including plots, are extracted — previously only
  markdown cells were read, so result figures were invisible to the pipeline.
- Provenance: commit SHA + date pinned per run; LabArchives entry metadata.
- AST-based dependency scanning; per-column CSV summaries; binary/HDF5/npy
  summaries; standalone `.py` scripts; multi-repo fetch.
- Deterministic link verification; critic four-outcome vocabulary.
- LabArchives crawler + corpus + identifier-aware search, and a LightRAG
  knowledge graph with incremental refresh and a warm in-listener service.
- On-demand figure fetch with zoom for precise plot reads.

### Correctness (Stage A)

- **The Slack summary is gated.** `report-writer` emits `slack_summary.md`;
  critic item 10 checks every number, unit, label pairing and hedge against the
  report; the skill posts it verbatim. Both factual defects that reached the lab
  came from freehand prose about a gated report.
- **Output directories cannot collide.** `--experiment-id` / `--out-dir`, plus a
  pre-flight refusal (exit 4) before any fetch is paid for.
- **Uploads update instead of duplicating**, take `--report-file`, and return the
  LabArchives location into `metadata.json`.
- **User-requested revisions re-enter the pipeline** — rebuilt from the extracted
  files, re-critiqued, never copied from the previous draft.

### Output format (Stage B)

- The plain-language template is now the **default**; the detailed one is opt-in.
- Provenance moved to a `provenance.md` sidecar, so the reader gets a clean
  document while every value keeps a checkable claim→source line.

### Tooling (Stage C)

- `lab_agent.cli.slack` — post / upload / read-thread / channels / search /
  fetch-files. Message text is always a file, never a shell argument. Every
  command verifies by reading back before reporting success.
- `check_env(live=True)` probes GitHub, Slack, LabArchives HMAC and the web
  cookie. "Set" is not "works".
- An agent-facing command table in `CLAUDE.md`, so a session stops
  reverse-engineering pipeline internals at runtime.

### Interaction (Stage D)

- Three-question intake replaces the unconditional DR question; the objective
  answer outranks anything inferred from code.
- The whole thread is read back after Phase A — replies sent mid-run never reach
  the running session otherwise.
- A caption-less file upload now wakes the bot.
- Per-user presentation preferences persist; a `slack-post` skill owns
  draft → approve → send.

### Q&A and housekeeping (Stage E and after)

- Questions naming a page read the crawled page directly instead of paying for a
  thin graph answer.
- `claude -p` auth failure is raised loudly instead of being returned as if it
  were the model's answer.
- A retention policy: the record is never auto-deleted, figures are reclaimed
  only once the report is uploaded **and** the run has gone idle, and anything
  being read is marked in use so it survives.
- Figures identified by LabArchives' own per-image id and by context, not by
  their position on a page.

### Known limitations shipping with v1

- **LabArchives re-save fails on uploaded reports.** Figures are still embedded
  as base64 `data:` URIs (`publish/labarchives.py:289`), which CKEditor rejects on
  a later save. Diagnosed; blocked on a DevTools trace of the manual re-save.
- **Phase D is non-deterministic.** Across three critic runs on near-identical
  content, items 1/5/10 were stable but 2 and 9 were flaky. One clean critique is
  not proof the gate always fires.
- **Cookie TTL** still expires inside a working session; self-refresh handles it
  but costs a Duo tap off campus.
- Two reports already in LabArchives carry known errors (a mislabelled amplitude,
  an invented figure detail, unsourced mechanism names).
- Figure identity by LabArchives id applies from the next crawl onward, not
  retroactively; four older runs were slimmed before hashes were recorded and are
  permanently unverifiable.
