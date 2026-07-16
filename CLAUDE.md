# LORE — Pipeline Instructions

When the user gives you a GitHub URL and LabArchives page names, run the full pipeline and write the report (Steps 1–4 below).

If the user asks for a **DR-only conditions report** (no GitHub URL, just a date or time window), skip directly to the DR-Only Report workflow at the bottom of this file.

**Where things live:**
- This file — the orchestration workflow (you, the main session, follow it).
- `.claude/agents/*.md` — subagent definitions for the report phases (github-analyst, labarchives-analyst, deps-analyst, dr-analyst, synthesis, report-writer, critic). Spawn them by name with the Agent tool.
- `docs/dr_physics_reference.md` — DR physics knowledge (read by dr-analyst).
- `docs/report_style_guide.md` — report structure and accuracy rules (read by report-writer).
- `lab_config.md` — lab-specific configuration (gitignored; see below).

## Before you begin — read lab_config.md

Read `lab_config.md` in the project root (same directory as this file). It contains all lab-specific configuration:

- `PROJECT_ROOT` — absolute path to this project on the current machine
- Required `.env` key names and their purposes
- LabArchives notebook names (`Primary notebook`, `Other notebooks`)
- `Upload folder` — the LabArchives folder where reports are posted
- `Wiring diagram page` — page title used for RF attenuation cross-check

Use these values wherever this file references `$PROJECT_ROOT`, `$PRIMARY_NOTEBOOK`, `$UPLOAD_FOLDER`, and `$WIRING_DIAGRAM_PAGE`. All `cd` commands use `$PROJECT_ROOT`.

## Agent capabilities — always available

You are a Claude Code agent with access to Slack and LabArchives. You can use these proactively to resolve ambiguous requests — do not immediately ask the user to clarify if you can figure it out yourself first.

### Slack (bot token in SLACK_BOT_TOKEN)

You can call the Slack API directly using the bot token. Available scopes include:
- `channels:history` / `groups:history` / `im:history` — read message history from channels and DMs
- `channels:read` / `groups:read` — list channels and get channel info
- `search:read` — search messages across the workspace

**When to use it:**
- User gives a vague reference ("the KID sweep from last Tuesday", "the measurement Axel posted about") → search Slack history for a GitHub URL, LabArchives page name, or experiment context
- User asks a question that might have been discussed in a channel → search before asking them to repeat themselves
- You need to know what was happening in the lab on a specific date → pull channel history from that day

**How to call the Slack API:**
```python
import os, requests
from dotenv import load_dotenv; load_dotenv()  # ensure .env is loaded regardless of how the session started
token = os.environ["SLACK_BOT_TOKEN"]
# Search messages
r = requests.get("https://slack.com/api/search.messages",
    headers={"Authorization": f"Bearer {token}"},
    params={"query": "KID sweep github.com", "count": 5})
print(r.json())

# Get channel history
r = requests.get("https://slack.com/api/conversations.history",
    headers={"Authorization": f"Bearer {token}"},
    params={"channel": "<channel_id>", "limit": 50})
print(r.json())
```

### LabArchives (already fully wired up)

You can search and read LabArchives pages directly via the `lab_agent.sources.labarchives` package. Use this when:
- User refers to an experiment by a vague name and you need to find the exact page title
- You want to cross-reference a new experiment against a previous one already in LabArchives
- You need context from adjacent notebook pages

**Resolution order for ambiguous requests:**
1. Search Slack history for relevant links or context
2. Search LabArchives by approximate page title
3. Only ask the user if both fail

## Step 1 — verify .env is populated

```bash
cd $PROJECT_ROOT
python -c "from lab_agent.config import check_env; check_env()"
```

Only report set or missing — never print the values themselves.
If any are missing, tell the user which ones to fill in before proceeding.

## Step 2 — run the pipeline

The GitHub URL is **optional**. Use whichever form fits:

```bash
cd $PROJECT_ROOT
# Full pipeline — GitHub + LabArchives:
python run.py "<github_url>" "<la_page_name_1>" "<la_page_name_2>"

# LabArchives-only — GitHub URL will be auto-discovered from page content if present:
python run.py "<la_page_name_1>" "<la_page_name_2>"
```

- LabArchives inputs can be page titles (case-insensitive), raw base64 tree_ids, or notebook URLs.
- If no GitHub URL is given and a GitHub link is found inside a LabArchives page, it is fetched automatically.
- Output lands in `outputs/<experiment_id>/` with:
  - `notebooks.md` — all notebook content
  - `labarchives.md` — lab notes from LabArchives
  - `data_summaries.md` — CSV contents and numeric ranges
  - `dependencies.md` — source code of lab-specific imports found in the org
  - `github_images/` + `github_images.md` — images downloaded from the GitHub repo
  - `labarchives_images/` + `labarchives_images.md` — images from LabArchives (attachments + embedded)
  - `dr_conditions.md` — dilution refrigerator temperatures and pressures (written only if the user requests it — see Step 2b)

**If run.py exits with `COOKIE_REFRESH_NEEDED`:** immediately run `python get_la_cookies.py` (never ask the user), then rerun the exact same `run.py` command. Do not proceed without images — the report requires them.

## Step 2b — ask about cryogenic data (always do this before writing the report)

After `run.py` finishes, **always** ask the user about DR conditions. Write the question as your **final text output and exit immediately** — the system delivers it to the user automatically. Do NOT post this via Python; do NOT add any preamble, explanation, or sign-off text around it.

Your final output should be exactly:

> Pipeline finished. Would you like to include dilution refrigerator conditions in this report?
> If yes, please give me the date and time window of your measurement (e.g. 'Feb 18 2025' or 'Feb 18 2025, 14:00–22:00').

Then exit. When the user replies, the next session will read the thread history to see the experiment ID and the user's answer, and continue from there.

**In the next session:** check the conversation history. The user's answer to the DR question is already there. Do NOT re-ask. Read the answer and continue:
- User said **yes** with a date/window → fetch DR data (below), then go to Step 3
- User said **no** → go directly to Step 3, skip DR entirely

If the user says **yes** and provides a date/window:

**If the user gives only a date** (e.g. "Feb 18 2025"), use `window_hours` — it extends ±N hours around the full calendar day:
```python
cd $PROJECT_ROOT
python -c "
from datetime import datetime
from lab_agent.dr_conditions import get_dr_conditions
md = get_dr_conditions('DR_DATA_PATH_FROM_ENV', datetime(YYYY, MM, DD), window_hours=12)
print(md if md else 'NO_DATA')
"
```

**If the user gives an explicit time range** (e.g. "Feb 18 2025, 14:00–22:00"), use `explicit_start` and `explicit_end` so the window is exact:
```python
cd $PROJECT_ROOT
python -c "
from datetime import datetime
from lab_agent.dr_conditions import get_dr_conditions
md = get_dr_conditions(
    'DR_DATA_PATH_FROM_ENV',
    datetime(YYYY, MM, DD),
    explicit_start=datetime(YYYY, MM, DD, HH, MM_start),
    explicit_end=datetime(YYYY, MM, DD, HH, MM_end),
)
print(md if md else 'NO_DATA')
"
```

Replace `DR_DATA_PATH_FROM_ENV` with the value of `DR_DATA_PATH` from `.env`. Save the output to `outputs/<experiment_id>/dr_conditions.md`.

If the user says **no**, skip entirely — do not mention DR conditions anywhere in the report.

## Step 3 — multi-agent report generation

After `run.py` finishes and DR conditions are decided (Step 2b), generate the report by spawning the project subagents defined in `.claude/agents/`. Do **not** write the report yourself.

Run the four phases in strict sequence: **Phase A** (analysts, concurrent) → **Phase B** (waits for all of A) → **Phase C** (waits for B) → **Phase D** (waits for C; may trigger one Phase C revision).

**Conventions for every subagent:**
- Let `<out_dir>` = `$PROJECT_ROOT/outputs/<experiment_id>`. Pass it literally in each spawn prompt — the agent definitions expect it.
- Each agent reads its inputs from `<out_dir>` and writes its output file(s) back into `<out_dir>`; the agent definitions carry the full role instructions and output schemas.
- Spawn agents with the Agent tool using the agent name as the subagent type. **Spawn the Phase A agents together in a single message so they run concurrently.** Wait for a phase to fully complete before starting the next.

> **This branch is agents-only.** A direct-API implementation of these same four phases (using `ANTHROPIC_API_KEY` instead of Claude Code subagents) exists separately on the `conference-api-version` branch as `run_phase_a/b/c/d.py`, for environments without a Claude Code plan. It is intentionally not part of this branch — always orchestrate the phases via the Agent tool as described below.

### Phase A — parallel extraction (concurrent subagents)

Spawn all applicable analysts in **one message**, each with the prompt `<out_dir> = <the actual path>`:

- **`github-analyst`** → writes `<out_dir>/extracted_github.md`
- **`labarchives-analyst`** → writes `<out_dir>/extracted_labarchives.md` (also fetches the wiring diagram page live)
- **`deps-analyst`** → writes `<out_dir>/extracted_deps.md`
- **`dr-analyst`** → writes `<out_dir>/extracted_dr.md` — **spawn only if `<out_dir>/dr_conditions.md` exists**

### Phase B — synthesis (1 subagent)

After all Phase A agents finish, spawn **`synthesis`** with the same `<out_dir>` prompt → writes `<out_dir>/connections.md`.

### Phase C — report writing (1 subagent)

After Phase B finishes, spawn **`report-writer`** with the `<out_dir>` prompt → writes `<out_dir>/[UNSIGNED] <experiment_id>.md`. It follows `docs/report_style_guide.md`.

### Phase D — critique and revision

After Phase C finishes, spawn **`critic`** with the `<out_dir>` prompt → writes `<out_dir>/critique.md`.

Read `critique.md`:
- **All items PASS** → proceed directly to Step 4 (upload).
- **Any item FAILs** → run one revision pass: spawn **`report-writer`** again with the prompt `<out_dir> = <path>. Revision mode — fix only the FAIL items in critique.md.` Then proceed to Step 4.

## Step 4 — upload the report to LabArchives

After the report passes critique, run:

```bash
cd $PROJECT_ROOT
python upload_to_labarchives.py outputs/<experiment_id>
```

This will:
1. Find the **$UPLOAD_FOLDER** folder in the $PRIMARY_NOTEBOOK notebook.
2. Create a new page named after the experiment (the output folder name).
3. Post the report as a rendered HTML rich-text entry.
4. Attach the raw `.md` file.

If the upload fails, report the error to the user — do not silently skip it.

## Known limitation
Web app page IDs (e.g. `11400322`) do **not** map to API tree_ids.
Pass page titles or base64 tree_ids instead.

---

## DR Quick Status (no report)

Use this when the user asks about **current** DR conditions — no date given, or phrased as "right now", "currently", "how's the DR", "what's the MXC temp", etc.

Do **not** write a report or upload anything. Just run the parser and reply in Slack with a short plain-text status.

### Steps

1. Run the parser with today's date and a 2-hour window:
   ```bash
   cd $PROJECT_ROOT
   python run_dr.py "YYYY-MM-DD" --hours 2
   ```
   (Replace YYYY-MM-DD with today's date.)

2. Read the output `dr_conditions.md`.

3. Reply to Slack with a brief status — 3–5 lines covering:
   - MXC temperature (min and current/latest reading)
   - Whether the system is at base, cooling, or warming
   - P1 pressure (pumps running or not)
   - Any anomaly worth flagging (thermal event, elevated temp, etc.)

No file is saved. No LabArchives upload. This is a read-only status check.

---

## DR-Only Report Workflow

Use this workflow when the user asks for a dilution refrigerator conditions report without any experiment (no GitHub URL). Examples:
- "Give me a DR report for Feb 18 2025"
- "What were the DR conditions on Feb 18 between 2pm and 10pm?"
- "Log the cooldown from Feb 17–19 2025"

### Step A — run the DR parser

```bash
cd $PROJECT_ROOT
python run_dr.py "YYYY-MM-DD"                        # ±12 h window (default)
python run_dr.py "YYYY-MM-DD" --hours 24             # wider window
python run_dr.py "YYYY-MM-DD HH:MM" "YYYY-MM-DD HH:MM"  # explicit start/end
```

This saves `dr_conditions.md` to `outputs/dr_YYYYMMDD/` and prints the output folder path.

### Step B — multi-agent report generation (DR-only)

Use a 3-agent pipeline — spawn agents sequentially (each waits for the previous), with `<out_dir>` = `$PROJECT_ROOT/outputs/dr_YYYYMMDD`:

1. **`dr-analyst`** — prompt: `<out_dir> = <path>`. Writes `extracted_dr.md` (reads `docs/dr_physics_reference.md` itself).
2. **`report-writer`** — prompt: `<out_dir> = <path>. This is a DR-only report (dr_YYYYMMDD).` Writes `[UNSIGNED] dr_YYYYMMDD.md` using the "DR-only report sections" structure in `docs/report_style_guide.md`.
3. **`critic`** — prompt: `<out_dir> = <path>. This is a DR-only report.` Uses its DR-only checklist. If any item fails, one revision pass with `report-writer` in revision mode.

### Step C — upload to LabArchives

```bash
cd $PROJECT_ROOT
python upload_to_labarchives.py outputs/dr_YYYYMMDD
```
