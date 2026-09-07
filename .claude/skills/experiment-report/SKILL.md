---
name: experiment-report
description: Generate and upload a full experiment report from a GitHub URL and/or LabArchives page names. Use when the user asks to "write a report", "run the pipeline", or gives a GitHub repo/tree URL plus one or more LabArchives page titles for an experiment. Runs run.py, the multi-agent Phase A–D pipeline, the structural gate, uploads to LabArchives, and logs timing + knowledge. NOT for knowledge/overview questions (use lab-qa) or dilution-refrigerator-only reports (use dr-report / dr-status).
---

# Experiment report pipeline (Steps 0–4)

Run this when the user gives a GitHub URL and/or LabArchives page names for an experiment and wants a report. Read `lab_config.md` first (per CLAUDE.md) for `$PROJECT_ROOT`, `$PRIMARY_NOTEBOOK`, `$UPLOAD_FOLDER`, `$WIRING_DIAGRAM_PAGE`. Do **not** write the report yourself — the subagents do.

## Step 0 — intake, up front (never wait for the answers)

Post these **immediately, before any fetching**, so the user answers while the pipeline runs:

> Starting now — three quick things you can answer while I work:
> 1. What question was this experiment trying to answer?
> 2. Want dilution refrigerator conditions included? (date/window, or "no")
> 3. Full report or brief?

- **Q1 is the important one.** The pipeline infers the objective from the notebook code, and code shows what was *run*, not what it was *for* — a calibration-looking notebook whose real purpose was comparing instrument noise produced two rewrite cycles. Whatever the user answers becomes the experiment's stated objective for Phase B and C.
- **Skip Q2 entirely for bench / room-temperature work** (spectrum analyser, VNA comparison, cabling, wiring) — there is no fridge involved, and asking anyway has stalled a pipeline for 12 minutes waiting on an answer that could not matter.
- **Q3** defaults to brief (see `docs/report_style_guide.md`); "full" selects the detailed template.
- Skip any part the original request already answered.
- **Slack session** (spawn prompt has a `Reply-to` line): post via `python -m lab_agent.cli.slack post`, then **continue straight to Step 1 without waiting**. Answers are collected in Step 2b.
- **Interactive session:** ask in chat and continue when the user answers.

## Step 1 — verify credentials actually work

```bash
cd $PROJECT_ROOT
python -c "from lab_agent.config import check_env; check_env(live=True)"
```

`live=True` asks GitHub, Slack and LabArchives whether each credential is accepted — "set" is not "works", and a token that was present but returning 401 once cost 75 minutes across six exchanges before anyone probed it.

Report only `set` / `MISSING` / `valid` / `INVALID` and the reason — **never** print a value, a length, or a prefix, and never paste one into Slack. If GITHUB_TOKEN is INVALID, say so and stop rather than starting a fetch that cannot succeed. An expired `LA_SESSION_COOKIE` is not a blocker here — Step 2 refreshes it automatically.

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
- **`--experiment-id <name>`** — pass this when the run's notebook lives inside *another* experiment's GitHub folder (a follow-up run committed alongside the original). The id is derived from that folder name, so without the flag this run writes into the earlier experiment's directory and overwrites its `metadata.json`. If you skip it, `run.py` now refuses with exit 4 and tells you what collided — give it a fresh `--experiment-id` rather than reaching for `--force`.
- If no GitHub URL is given and a GitHub link is found inside a LabArchives page, it is fetched automatically.
- Output lands in `outputs/<experiment_id>/`: `notebooks.md`, `labarchives.md`, `data_summaries.md`, `dependencies.md`, `github_images/` + `github_images.md`, `labarchives_images/` + `labarchives_images.md`, and (only if requested) `dr_conditions.md`.

**If run.py exits with `COOKIE_REFRESH_NEEDED`:** immediately run `python get_la_cookies.py` (never ask the user), then rerun the exact same `run.py` command. Do not proceed without images.

## Step 2b — collect everything the user said while you worked (after Phase A, never before)

Do **not** block Steps 2–3 on the intake answers — go straight from `run.py` to Phase A. When Phase A finishes, read the whole thread back:

```bash
python -m lab_agent.cli.slack read-thread --channel <channel> --thread <thread_ts>
```

**Messages sent while a session is running are never delivered to it** — the listener absorbs replies into the running thread. This read is the only way you will ever see them, and skipping it means an instruction the user watched you acknowledge ("you only need the two plots from the No Filter notebook") is silently dropped while your next post says "Writing report…".

Handle every message since you started, not just the DR line:

- **Q1 objective** → this is the experiment's stated goal; pass it verbatim to `synthesis` and `report-writer`.
- **Q3 full/brief** → pass to `report-writer`.
- **Any other instruction** ("only use these figures", "drop that section", "the cause was actually X") → apply it, and **acknowledge it in your next Slack post** so the user can see it landed. If you cannot apply it, say why. Never let one pass unmentioned.
- **Interactive session:** the answers are already in the conversation.

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

**If a phase agent returns nothing.** A spawned agent can come back empty — one figure-read
agent's output file was still 0 bytes three minutes after its parent had given up on it.
When that happens: check whether the file it was supposed to write exists, re-spawn **once**,
and if it is still empty, say so and stop. Do **not** quietly do the phase's work yourself —
that is precisely how a report reached LabArchives without ever being critiqued.

### Phase B — synthesis (1 subagent)

After all Phase A agents finish **and the DR answer is resolved**, spawn **`synthesis`** with the `<out_dir>` prompt → `<out_dir>/connections.md`.

### Phase C — report writing (1 subagent)

After Phase B, spawn **`report-writer`** with the `<out_dir>` prompt → `<out_dir>/[UNSIGNED] <experiment_id>.md`. It follows `docs/report_style_guide.md` and writes `provenance.md` + `slack_summary.md` alongside.

Include in the spawn prompt, when you have them:
- **the stated objective** from intake Q1 — it outranks anything inferred from notebook code;
- **`full`** if the user asked for a full/detailed report (otherwise the brief template is the default);
- **standing preferences** for this user:

```bash
python -m lab_agent.cli.preferences show --user <slack_user_id>
```

Paste any output into the spawn prompt verbatim. These are presentation-only preferences the user has already stated on an earlier run ("brief", "no Key Parameters table", "dBm only") — re-establishing them every time is a cost they should only pay once.

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

This finds the **$UPLOAD_FOLDER** folder in the **$PRIMARY_NOTEBOOK** notebook, posts the report as rendered HTML, and attaches the raw `.md`. If a page with that title already exists it adds a **revision entry to that page** rather than creating a duplicate. If the upload fails, report the error — do not silently skip it.

Options when you need them:
- `--report-file "<name>.md"` — **required whenever the directory holds more than one report** (e.g. a full report and a plain-language variant). Without it the newest by mtime wins, which is a coin flip.
- `--page-title "<title>"` — for a variant that should live on its own page (e.g. `[UNSIGNED] <experiment_id>_simple`).
- `--new-page` — force a fresh page. Rarely correct; the default already avoids duplicates.

The command prints the notebook URL and the folder/page it wrote to, and records the same under `labarchives_upload` in `metadata.json`. **Cite that** when telling the user where the report is. LabArchives web page IDs do not map to API tree_ids, so there is no per-page deep link — give the notebook URL plus "$UPLOAD_FOLDER / \<page title\>". Never invent or abbreviate a URL.

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

## Step 5 — post the Slack summary (do NOT write your own)

`report-writer` produced `<out_dir>/slack_summary.md`, and the critic checked its numbers and hedging against the report. **Post that file's contents as your final message, verbatim**, adding only the `<@user>` tag and the one-line timing summary.

Do not compose a fresh summary from what the subagents told you. Every factual defect that has reached the lab came from that habit — a report saying "+0.165 dB at 6.9 GHz, −0.139 dB at 6.44 GHz" became "−0.139 dB at 6.9 GHz and +0.165 dB at 6.44 GHz" in the Slack message, and a report whose "confirming" the critic had just removed was announced as "confirmed". The report is gated; freehand prose about it is not.

If `slack_summary.md` is missing, that is an `[ERROR]` from the structural gate — re-spawn `report-writer` rather than writing one yourself.

## Handling a user-requested revision (after the report exists)

When the user asks for a change to a report that is already written or uploaded ("make it shorter", "drop the key parameters", "the goal was really X", "use my photo"), **do not edit or rewrite the report yourself.** That path bypasses the critic entirely, and rewriting from the previous draft violates the "never read `[UNSIGNED]` files as source material" rule in `docs/report_style_guide.md`.

1. Spawn **`report-writer`** with: `<out_dir> = <path>. User revision — <the user's request, verbatim>.` It rebuilds from the extracted files, not from the old draft.
2. Re-spawn **`critic`**, and handle its verdict exactly as in Phase D.
3. Re-run the structural gate, then re-upload with `python upload_to_labarchives.py outputs/<experiment_id>` — which now revises the existing page instead of creating a duplicate.

If the user wants a **separate** document rather than a replacement (e.g. "make a simpler version *as well*"), pass the target filename to `report-writer` and upload it with `--report-file` and its own `--page-title`.

**Record durable preferences.** When the request expresses a standing preference about *presentation* rather than this one report — "keep them shorter", "we don't use the Key Parameters table", "always dBm" — save it so the next run starts there:

```bash
python -m lab_agent.cli.preferences set --user <slack_user_id> --note "<the preference, one line>"
```

Only presentation. Never record anything that changes which numbers are reported, how strongly a claim is stated, or what the critic checks. A one-off ("drop section 4 from *this* report") is not a preference.

## Known limitation
Web app page IDs (e.g. `11400322`) do **not** map to API tree_ids. Pass page titles or base64 tree_ids instead.
