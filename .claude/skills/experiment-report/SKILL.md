---
name: experiment-report
description: Generate and upload a full experiment report from a GitHub URL and/or LabArchives page names. Use when the user asks to "write a report", "run the pipeline", or gives a GitHub repo/tree URL plus one or more LabArchives page titles for an experiment. Runs run.py, the multi-agent Phase A–D pipeline, the structural gate, uploads to LabArchives, and logs timing + knowledge. NOT for knowledge/overview questions (use lab-qa) or dilution-refrigerator-only reports (use dr-report / dr-status).
---

# Experiment report pipeline (Steps 0–4)

Run this when the user gives a GitHub URL and/or LabArchives page names for an experiment and wants a report. Read `lab_config.md` first (per CLAUDE.md) for `$PROJECT_ROOT`, `$PRIMARY_NOTEBOOK`, `$UPLOAD_FOLDER`, `$WIRING_DIAGRAM_PAGE`. Do **not** write the report yourself — the subagents do.

## Step 0 — ask the DR question up front (never wait for the answer)

Ask about dilution refrigerator conditions **immediately, before any fetching**, so the user can answer while the pipeline runs:

> While I fetch the data — would you like dilution refrigerator conditions included in this report?
> If yes, reply with the date and time window of your measurement (e.g. 'Feb 18 2025' or 'Feb 18 2025, 14:00–22:00'). If not, just say "no".

- **Slack session** (spawn prompt has a `Reply-to` line): post the question to the thread via `chat.postMessage`, then **continue straight to Step 1 without waiting**. The answer is collected later, in Step 2b.
- **Interactive session:** ask in chat and continue when the user answers.
- If the original request already answers it ("include DR for Feb 18", "no DR needed"), skip the question and treat it as resolved.

## Step 1 — verify .env is populated

```bash
cd $PROJECT_ROOT
python -c "from lab_agent.config import check_env; check_env()"
```

Only report set or missing — never print the values themselves. If any are missing, tell the user which ones to fill in before proceeding.

## Step 2 — run the pipeline

The GitHub URL is **optional**.

```bash
cd $PROJECT_ROOT
# Full pipeline — GitHub + LabArchives:
python run.py "<github_url>" "<la_page_name_1>" "<la_page_name_2>"

# LabArchives-only — GitHub URL auto-discovered from page content if present:
python run.py "<la_page_name_1>" "<la_page_name_2>"
```

- LabArchives inputs: page titles (case-insensitive), raw base64 tree_ids, or notebook URLs.
- If no GitHub URL is given and a GitHub link is found inside a LabArchives page, it is fetched automatically.
- Output lands in `outputs/<experiment_id>/`: `notebooks.md`, `labarchives.md`, `data_summaries.md`, `dependencies.md`, `github_images/` + `github_images.md`, `labarchives_images/` + `labarchives_images.md`, and (only if requested) `dr_conditions.md`.

**If run.py exits with `COOKIE_REFRESH_NEEDED`:** immediately run `python get_la_cookies.py` (never ask the user), then rerun the exact same `run.py` command. Do not proceed without images.

## Step 2b — resolve the DR answer (checked after Phase A, never before)

Do **not** block Steps 2–3 on the DR answer — go straight from `run.py` to Phase A. Check for the answer only when Phase A finishes, right before Phase B:

- **Slack session:** fetch the thread with `conversations.replies` (channel + thread_ts from the `Reply-to` line) and look for the user's reply after the DR question.
- **Interactive session:** the answer is already in the conversation.

Then:
- **yes** with a date/window → fetch DR data (below), spawn `dr-analyst`, wait, then proceed to Phase B.
- **no** → proceed directly to Phase B; do not mention DR conditions anywhere in the report.
- **No answer yet** (Slack only) → write this reminder as your final text output and exit: *"The report analysis is ready — I just need your answer on dilution refrigerator conditions to finish. Reply with a date/time window to include them, or 'no' to skip."* The next session picks up from here: outputs and `extracted_*.md` already exist, so it must NOT re-run `run.py` or Phase A — it resolves the DR answer and continues from Phase B.

If **yes** with a date only (e.g. "Feb 18 2025"), use `window_hours` (±N hours around the full calendar day):
```python
cd $PROJECT_ROOT
python -c "
from datetime import datetime
from lab_agent.dr import get_dr_conditions
md = get_dr_conditions('DR_DATA_PATH_FROM_ENV', datetime(YYYY, MM, DD), window_hours=12)
print(md if md else 'NO_DATA')
"
```

If **yes** with an explicit time range (e.g. "Feb 18 2025, 14:00–22:00"), use `explicit_start`/`explicit_end`:
```python
cd $PROJECT_ROOT
python -c "
from datetime import datetime
from lab_agent.dr import get_dr_conditions
md = get_dr_conditions(
    'DR_DATA_PATH_FROM_ENV',
    datetime(YYYY, MM, DD),
    explicit_start=datetime(YYYY, MM, DD, HH, MM_start),
    explicit_end=datetime(YYYY, MM, DD, HH, MM_end),
)
print(md if md else 'NO_DATA')
"
```

Replace `DR_DATA_PATH_FROM_ENV` with `DR_DATA_PATH` from `.env`. Save the output to `outputs/<experiment_id>/dr_conditions.md`.

## Step 3 — multi-agent report generation

**Immediately after `run.py` finishes** — do not wait for the DR answer — generate the report by spawning the project subagents in `.claude/agents/`. Do **not** write the report yourself.

Phase order: **Phase A** (analysts, concurrent; starts right after run.py) → **resolve DR answer (Step 2b)**, spawning `dr-analyst` if needed → **Phase B** (waits for all extractions) → **Phase C** (waits for B) → **Phase D** (waits for C; may trigger one Phase C revision).

Conventions: `<out_dir>` = `$PROJECT_ROOT/outputs/<experiment_id>`; pass it literally in every spawn prompt. Each agent reads inputs from `<out_dir>` and writes output back into it. Spawn Phase A agents together in a **single message** so they run concurrently. Wait for a phase to fully complete before the next.

> This branch is agents-only. Always orchestrate via the Agent tool (the direct-API `run_phase_*.py` implementation lives on `conference-api-version`, not here).

**Before Phase A, verify links** (deterministic, so the report never cites a dead source):

```bash
cd $PROJECT_ROOT
python -m lab_agent.cli.verify_links outputs/<experiment_id>
```

Writes `link_check.md`. The labarchives-analyst carries each link's status; the report-writer must not cite a `not_found`/`unreachable` link (writes `[MISSING: <url>]`); the critic checks this (item 8).

### Phase A — parallel extraction (concurrent subagents)

Spawn in **one message, immediately after run.py**, each with `<out_dir> = <the actual path>`:
- **`github-analyst`** → `<out_dir>/extracted_github.md`
- **`labarchives-analyst`** → `<out_dir>/extracted_labarchives.md` (also fetches the wiring diagram page live)
- **`deps-analyst`** → `<out_dir>/extracted_deps.md`

When they finish, resolve the DR answer (Step 2b). If yes, fetch DR data and spawn:
- **`dr-analyst`** → `<out_dir>/extracted_dr.md` — **spawn only once `<out_dir>/dr_conditions.md` exists**.

### Phase B — synthesis (1 subagent)

After all Phase A agents finish **and the DR answer is resolved**, spawn **`synthesis`** with the `<out_dir>` prompt → `<out_dir>/connections.md`.

### Phase C — report writing (1 subagent)

After Phase B, spawn **`report-writer`** with the `<out_dir>` prompt → `<out_dir>/[UNSIGNED] <experiment_id>.md`. It follows `docs/report_style_guide.md`.

### Phase D — critique and revision

After Phase C, spawn **`critic`** with the `<out_dir>` prompt → `<out_dir>/critique.md`.

Read the `## Summary` line in `critique.md`:
- **`passed`** → proceed to the structural check.
- **`gaps_found: <items>`** → one revision pass: spawn **`report-writer`** with `<out_dir> = <path>. Revision mode — fix only the FAIL items in critique.md.` Re-spawn the critic; if still not `passed`, escalate to the user rather than looping again.
- **`expert_needed: <question>`** or **`human_needed: <what is broken>`** → do **not** upload. Write the critic's specific question(s) as your final text output and stop.

**Deterministic structural gate (before upload):**

```bash
cd $PROJECT_ROOT
python -m lab_agent.cli.eval check outputs/<experiment_id>
```

Validates required `##` sections in every `extracted_*.md`, the Key Parameters table appearing exactly once with a Source column, and every embedded image path resolving to a real file. Fix any `[ERROR]` before uploading; `[WARN]` is advisory.

## Step 4 — upload the report to LabArchives

After the report passes critique:

```bash
cd $PROJECT_ROOT
python upload_to_labarchives.py outputs/<experiment_id>
```

This finds the **$UPLOAD_FOLDER** folder in the **$PRIMARY_NOTEBOOK** notebook, creates a page named after the experiment, posts the report as rendered HTML, and attaches the raw `.md`. If the upload fails, report the error — do not silently skip it.

**After a successful upload, log latency:**

```bash
cd $PROJECT_ROOT
python -m lab_agent.cli.timings outputs/<experiment_id>
```

Include a one-line timing summary (total minutes, slowest phase) in your final Slack message.

**Also record the experiment into the knowledge bundle:**

```bash
cd $PROJECT_ROOT
python -m lab_agent.cli.record_knowledge outputs/<experiment_id>
```

Distils a per-experiment concept into `knowledge/experiments/<id>.md`. When a human later signs off, re-run with `--sign` to promote it to a high-trust exemplar. The knowledge bundle is a finding aid only — never a source of new report numbers.

## Known limitation
Web app page IDs (e.g. `11400322`) do **not** map to API tree_ids. Pass page titles or base64 tree_ids instead.
