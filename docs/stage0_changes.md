# Stage 0 Changes — Foundations & Quick Wins

In-depth summary of the Stage 0 work from the [roadmap](../ROADMAP.md), with a
before → after comparison against the previous system. Stage 0 is the "foundations
and quick wins" tier: fix an active bug, harden a security-adjacent path, and stand
up the safety nets (eval + observability) *before* the deeper extraction work
(Stage 1+) that will change what the pipeline reads.

**Nothing here changes report content or physics.** These are input-normalization,
measurement, verification, and a new lightweight question-answering path — all
additive, all tested, none touching the four report phases' logic.

Date: 2026-07-30. Branch: `main` (uncommitted working tree).

---

## At a glance

| # | Change | Goal served | Old system | New system |
|---|--------|-------------|-----------|-----------|
| 1 | Slack-link fix + link-security | #1 ingest, security | GitHub links sent via Slack silently misrouted | Links normalized at the edge; auto-discovery host/org-gated + capped |
| 2 | Observability | #2 timely | Runtime a black box (~17 min, unmeasured) | Per-stage fetch timing + per-phase timeline from file mtimes |
| 3 | Eval harness | all (regression guard) | No automated regression catch; prompt/extraction changes flew blind | Layered `check` (structure) + `diff` (golden) + fixture tests |
| 4 | Q&A fast-path | #4 answer questions | Q&A leaned on lossy Slack search; no cross-experiment lookup | Local ranked search over past outputs with experiment-id provenance |

Four goals, per the project mission: (1) ingest all experiment content, (2)
efficiently/timely, (3) output to LabArchives, (4) answer lab/experiment questions
on Slack.

---

## 1. Slack-link fix + link-security hardening

**The bug (reported by the lab).** A GitHub folder URL sent through Slack didn't
work, while the same URL embedded in a LabArchives page did.

**Root cause.** Slack auto-formats URLs in a message's `text` as `<https://github.com/…>`
(or `<url|label>`) and HTML-escapes `&`/`<`/`>`. That raw text was passed straight
into the spawned agent's prompt (`slack/sessions.py`) and into the fetched thread
history (`slack/history.py`) with **no unwrapping** anywhere. So the agent saw
`<https://github.com/…>`; the leading `<` means the string no longer starts with
`http`, which breaks `run.py`'s argument routing (`args[0].startswith("http")`) and
the anchored URL regex in `parse_github_url` — the link was misrouted as a
LabArchives page name.

**Before → after.**

| | Old | New |
|---|-----|-----|
| Slack URL handling | raw `<…>` reached the agent/`run.py` | `unwrap_slack_text()` strips `<…>`/`<url\|label>` and unescapes entities at the edge |
| Applied where | nowhere | both listener handlers + history assembly |
| Mentions | n/a | `<@U…>`, `<#C…\|name>`, `<!here>` deliberately left intact |
| Auto-discovery of GitHub links in LabArchives | any `github.com` URL, unbounded, first one fetched | host allowlist (github.com), optional org allowlist (`GitHub orgs` config key), 10-URL cap — enforced **in the tool**, not the prompt |

**Why it's better.** The fix is deterministic and lives at the trust boundary (the
listener), so *every* downstream consumer sees clean text — not just this one bug.
The link-security piece is Fable's #10 recommendation ("hard caps + allowed-host
filters inside tools, not prompts") implemented literally: auto-fetching a URL found
in untrusted LabArchives content is now gated by host and (optionally) owner, and
capped, reducing SSRF-style / wrong-repo risk.

**Files.** `slack/api.py` (new `unwrap_slack_text`), `slack/listener.py`,
`slack/history.py`, `cli/run_pipeline.py` (`_github_url_allowed`, `_allowed_github_orgs`,
gated/capped `_find_github_urls`), `lab_config.template.md` (documents `GitHub orgs`).
**Tests.** `tests/test_slack_unwrap.py` (9), plus inline verification of the gate.

---

## 2. Observability (timing)

**Before.** The pipeline ran ~17 min end-to-end with no breakdown — no way to see
which stage dominated or whether a change made things slower. "Timely" (goal #2) was
a goal we couldn't measure. This matters right before Stage 1, where WS1 (capturing
full notebook code + cell-output plots) will enlarge `notebooks.md` and is the change
most likely to grow latency.

**After.**
- **`run.py` fetch stages** are wall-clock timed (`run_pipeline.run`), printed and
  stored in `metadata.json` under `fetch_timings_sec` (LabArchives fetch, GitHub
  fetch, summarize, write+deps, total).
- **The four report phases (A–D)** run as LLM-spawned Agent-tool subagents, so they
  can't be timed inside Python. `lab_agent/cli/timings.py` derives a **per-phase
  timeline from output-file mtimes** — deterministic, zero LLM reliance. Verified on
  real runs: it corroborates the ~17 min total and pinpoints **Phase D (critique) as
  the largest single block (~564 s)**, and even reveals when an edit-in-place
  revision occurred (the report's mtime lands after the critique's).
- Wired into `CLAUDE.md` Step 4 so each run surfaces its latency in Slack.

**Honest limitation.** Per-phase **token** counts are *not* included: Claude Code
plan subagents don't expose token usage to the orchestrator or to Python. Timing is
the actionable latency signal; token accounting would need Claude Code's own usage
reporting. Flagged rather than faked.

**Files.** `cli/run_pipeline.py`, new `cli/timings.py`, `CLAUDE.md` Step 4.

---

## 3. Eval harness (layered)

**Before.** LORE's behavior lives largely in editable prompts (`.claude/agents/*.md`,
`docs/report_style_guide.md`) and extraction code. There was **no automated way to
catch regressions** — a memory note even records a critic self-contradiction that
silently forced needless revisions, exactly the class of bug this catches. We are
about to change extraction *and* seven agent prompts in Stage 1; blind is not an
option.

**After — one harness, three layers, because LORE produces two kinds of artifacts:**

| Artifact kind | Examples | Stable run-to-run? | Tool |
|---|---|---|---|
| Deterministic (Python) | `notebooks.md`, `metadata.json`, `data_summaries.md`, `dependencies.md`, `labarchives.md` | yes | **`diff`** — golden byte/JSON compare (`--update` re-baselines; volatile fetch timings ignored) |
| Non-deterministic (LLM) | `extracted_*.md`, the report, `critique.md` | no (wording varies) | **`check`** — structural invariants |

- **`check`** (`python -m lab_agent.cli.eval check <dir>`) validates: required `##`
  sections in every extracted/synthesis file (from the agent schemas), the Key
  Parameters table appears exactly once with a Source column, critique has a verdict,
  and — highest value — **every embedded image path resolves to a real file**, which
  catches the historical `github_images/` vs `labarchives_images/` prefix bug.
- **`diff`** (`python -m lab_agent.cli.eval diff <dir>`) compares the deterministic
  files against a golden snapshot in `tests/golden/<exp>/`, ignoring volatile keys so
  an unchanged run diffs clean. This is Fable's #12 verification ("diff `notebooks.md`
  /`metadata.json` against prior outputs").
- **Fixture tests** land with each future workstream (WS1 serializer, WS5 AST, WS6 CSV).

Why the split is *correct*, not a compromise: a golden diff on LLM outputs would be
flaky (wording changes every run); a structural checker on deterministic outputs
would miss value drift. Each layer owns the failure class it's actually good at.

**Validated on the real archive.** Running `check` over three past experiments
surfaced genuine signal — older runs legitimately lacking a Source column /
`metadata.json` / a critique Summary — and, after fixing a false positive in the
image-path parser (filenames with spaces/parens like `image (1).png`),
`power_calibration_20260227` passes clean. A golden baseline for it is committed under
`tests/golden/`. Wired into `CLAUDE.md` Step 3/4 as a deterministic **pre-upload gate**.

**Files.** new `cli/eval.py`, `tests/golden/power_calibration_20260227/`,
`tests/test_eval_harness.py` (6), `CLAUDE.md` Step 3/4.

---

## 4. Q&A fast-path

**Before.** LORE was ~80% a report generator. "Answer other lab/experiment questions
on Slack" (a co-equal goal) had no real support: a question spawned the same heavy
session, and cross-experiment lookups fell back to lossy Slack search with no
structured access to what past reports already established.

**After.** `lab_agent/cli/ask.py` is a lightweight local search over the
already-produced, normalized markdown under `outputs/<exp>/` (`extracted_*.md`,
`connections.md`, `dr_conditions.md`, the report). It ranks by query-term overlap
(with an experiment-id match bonus), returns the top snippets **each tagged with its
source experiment id**, and deliberately excludes raw dumps (`notebooks.md`,
`data_summaries.md`) as noisy and the agent memory (private notes) as out of scope.

Verified against the real archive:
- *"what attenuation were we running"* → the −90 dB chain across the cooldown/QPD runs,
  including the mid-run modifications.
- *"DAC current amplitude power calibration"* → `power_calibration_20260227`, with the
  exact DAC_CURRENT sweep (24,000–40,500 µA) and saturation amplitude ~0.599.

Wired into `CLAUDE.md` as a **fast-path**: for a question (no GitHub URL, not a DR
report), the session runs `ask`, answers **citing experiment id(s)**, **never
fabricates numbers**, and does **not** run `run.py` or the report phases. Resolution
order for ambiguous requests now starts with this local search before Slack/LabArchives.
A pointer was added to the session spawn prompt so a fresh question reliably takes
this path.

**Why it's better.** Immediate improvement to goal #4 with zero new infrastructure,
respecting the trust model (retrieval is for *finding/citing* existing report
content, never generating new numbers). It is explicitly the Stage-0 stand-in for the
OKF knowledge bundle (Stage 4) and later LightRAG (Stage 5) over the same corpus.

**Files.** new `cli/ask.py`, `CLAUDE.md` (fast-path section + resolution order),
`slack/sessions.py` (spawn-prompt pointer), `tests/test_ask_search.py` (4).

---

## Testing

All checks green (run from the repo root with the root on `PYTHONPATH`):

```bash
python tests/test_slack_unwrap.py     # 9 pass — Slack unwrap + entity handling
python tests/test_eval_harness.py     # 6 pass — check invariants + golden diff
python tests/test_ask_search.py       # 4 pass — Q&A relevance/provenance/scope
python -m lab_agent.cli.eval check outputs/power_calibration_20260227   # clean
```

Additionally verified: the entire `lab_agent` package tree (31 modules) imports with
no errors, and `git status` shows only the intended additions/edits (no deletions).

---

## Scope & what's next

- **Not touched:** the four report phases' logic, physics reference, upload path.
- **Deferred honestly:** per-phase token accounting (not exposed by Claude Code plan
  sessions).
- **Next (Stage 1):** the extraction-depth work — WS1 notebook code + **cell-output
  plots** (fixes the "doesn't understand plots in notebooks" bug), WS2 provenance,
  WS5 AST dependency extraction, WS6 CSV summaries, plus OKF Phase 1 frontmatter.
  Every one of those now runs behind the eval harness built here.

See [ROADMAP.md](../ROADMAP.md) for the full Stage 1–5 plan.
