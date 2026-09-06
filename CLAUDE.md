# LORE — Pipeline Instructions

You are the main session. Read `lab_config.md`, figure out which of the four request types this is, then **invoke the matching skill** — each skill carries the full step-by-step workflow so only the one you need is loaded.

**Where things live:**
- This file — the router (read lab config, classify the request, invoke the right skill) plus the always-available agent capabilities below.
- `.claude/skills/*/SKILL.md` — the workflows themselves. Invoke by name with the Skill tool:
  - **`experiment-report`** — full pipeline for a GitHub URL and/or LabArchives page names (report → LabArchives upload).
  - **`lab-qa`** — answer a knowledge/overview question (or figure/plot/value question) from the local knowledge graph. Local, no cookies for text.
  - **`dr-report`** — standalone dilution-refrigerator conditions report for a past date/window (no experiment).
  - **`dr-status`** — quick current DR status ("how's the DR right now?"), read-only, no upload.
  - Capability skills (invoked by `lab-qa`, or directly if the user asks for exactly this): **`find-device-notes`** (map a chip/run/sample ID or vague reference to its LabArchives page + notes; local, free) and **`deduce-figure`** (fetch a page's figures and read/interpret a specific plot/value; cookie-gated).
- `.claude/agents/*.md` — subagent definitions for the report phases (github-analyst, labarchives-analyst, deps-analyst, dr-analyst, synthesis, report-writer, critic). Spawn them by name with the Agent tool (the skills tell you when).
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

Use these values wherever a skill references `$PROJECT_ROOT`, `$PRIMARY_NOTEBOOK`, `$UPLOAD_FOLDER`, and `$WIRING_DIAGRAM_PAGE`. All `cd` commands use `$PROJECT_ROOT`.

## Which skill to invoke — classify first

Decide the request type, then invoke that skill (do the ambiguity-resolution below first if the request is vague):

| The message is… | Invoke |
|---|---|
| A request to **write/run a report** with a GitHub URL and/or LabArchives page names for an experiment | **`experiment-report`** |
| A **question / knowledge query** about past work — "what do we know about…", "have we ever…", "which experiments/runs…", an overview/topic question, or a figure/plot/value question | **`lab-qa`** |
| A request for a **DR conditions report** for a past date or time window, with no experiment | **`dr-report`** |
| A question about **current** DR conditions — "right now", "how's the DR", "what's the MXC temp" | **`dr-status`** |

When in doubt between a report and a question: if the user wants a document produced and uploaded, it's a report; if they want an answer, it's `lab-qa`.

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
1. Query the knowledge graph: `python -m lab_agent.cli.query_kb "<the request>"` — a LightRAG graph over **all crawled LabArchives pages + past reports** (local; no cookies). It falls back to keyword search if the graph isn't built.
2. Search Slack history for relevant links or context
3. Search LabArchives by approximate page title
4. Only ask the user if all three fail

## Known limitation
Web app page IDs (e.g. `11400322`) do **not** map to API tree_ids. Pass page titles or base64 tree_ids instead.
