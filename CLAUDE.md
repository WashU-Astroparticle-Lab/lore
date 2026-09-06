# LORE — Pipeline Instructions

You are the main session. Read `lab_config.md`, figure out which request type this is, then **invoke the matching skill** — each skill carries the full step-by-step workflow so only the one you need is loaded.

**Where things live:**
- This file — the router (read lab config, classify the request, invoke the right skill) plus the always-available agent capabilities below.
- `.claude/skills/*/SKILL.md` — the workflows themselves. Invoke by name with the Skill tool:
  - **`experiment-report`** — full pipeline for a GitHub URL and/or LabArchives page names (report → LabArchives upload).
  - **`lab-qa`** — answer a knowledge/overview question (or figure/plot/value question) from the local knowledge graph. Local, no cookies for text.
  - **`dr-report`** — standalone dilution-refrigerator conditions report for a past date/window (no experiment).
  - **`dr-status`** — quick current DR status ("how's the DR right now?"), read-only, no upload.
  - **`slack-post`** — draft, get approval for, and send a Slack message to a channel with figures attached ("post this to the group", "send a summary to #channel").
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
| A request to **send/post a message to a Slack channel** — "post this to the group", "share these plots in #channel" | **`slack-post`** |

When in doubt between a report and a question: if the user wants a document produced and uploaded, it's a report; if they want an answer, it's `lab-qa`.

## Agent capabilities — always available

You are a Claude Code agent with access to Slack and LabArchives. You can use these proactively to resolve ambiguous requests — do not immediately ask the user to clarify if you can figure it out yourself first.

### Slack (bot token in SLACK_BOT_TOKEN)

You can call the Slack API directly using the bot token. Available scopes include:
- `channels:history` / `groups:history` / `im:history` — read message history from channels and DMs
- `channels:read` — list public channels. NOTE: `groups:read` is NOT currently granted, so `conversations.list` fails with `missing_scope`; the `channels` command will say so. Ask the user to name the channel until that scope is added.
- `search:read` — search messages across the workspace

**When to use it:**
- User gives a vague reference ("the KID sweep from last Tuesday", "the measurement Axel posted about") → search Slack history for a GitHub URL, LabArchives page name, or experiment context
- User asks a question that might have been discussed in a channel → search before asking them to repeat themselves
- You need to know what was happening in the lab on a specific date → pull channel history from that day

**How to talk to Slack — use the CLI, never `python -c`:**

```bash
cd $PROJECT_ROOT
python -m lab_agent.cli.slack post        --channel <C…> [--thread <ts>] --text-file <path>
python -m lab_agent.cli.slack upload      --channel <C…> [--thread <ts>] --file <path> [--file <path>] [--comment-file <path>]
python -m lab_agent.cli.slack read-thread --channel <C…> --thread <ts> [--limit 50]
python -m lab_agent.cli.slack channels    [--filter <substring>]
```

**Message text is passed as a FILE, never as a shell argument.** Write the message with the Write tool, then point `--text-file` at it. Putting message text in a `python -c` string or a shell argument is how backticks in a message got executed by bash — real words vanished from messages users received, and one failure printed a session cookie into the log.

Every command **verifies by reading back** what it did and exits non-zero if it cannot. Exit 0 means delivered *and confirmed*. Never tell the user something was sent or attached unless the command exited 0 — `ok: true` from a raw API call is not proof, and announcing unsent images cost four round-trips in one real thread.

For anything the CLI does not cover (e.g. `search.messages`), you may call the API directly — but use `lab_agent.slack.api.api_get` / `api_post`, which raise on `ok: false` instead of failing silently:

```bash
python -c "from lab_agent.slack.api import api_get; print(api_get('search.messages', {'query': 'KID sweep github.com', 'count': '5'}))"
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

## Agent-facing API — the supported commands

This is the complete set. **Do not read pipeline source (`run_pipeline.py`, `github.py`, `upload.py`, `publish/labarchives.py`, the LightRAG KV stores) to work out how to do something** — every capability is below. If none of them fits, say so and ask; improvising against internals is slow, breaks on refactors, and puts a production session one step from editing pipeline code.

All run from `$PROJECT_ROOT`.

| Need | Command |
|---|---|
| Check credentials are present | `python -c "from lab_agent.config import check_env; check_env()"` |
| Check credentials actually **work** | `python -c "from lab_agent.config import check_env; check_env(live=True)"` |
| Fetch an experiment | `python run.py "<github_url>" "<la_page>"…` — `--experiment-id NAME` when the code lives in another run's folder, `--reuse`/`--force` to override the collision guard |
| Upload a report | `python upload_to_labarchives.py outputs/<id>` — `--report-file`, `--page-title`, `--new-page` |
| Structural gate / drift | `python -m lab_agent.cli.eval check\|diff outputs/<id>` |
| Report timings | `python -m lab_agent.cli.timings outputs/<id>` |
| Record into the knowledge bundle | `python -m lab_agent.cli.record_knowledge outputs/<id> [--sign]` |
| Verify links in a run | `python -m lab_agent.cli.verify_links outputs/<id>` |
| Ask the knowledge graph | `python -m lab_agent.cli.query_kb "<question>"` |
| Keyword/identifier search (free) | `python -m lab_agent.cli.ask "<question>"` |
| Read one crawled page verbatim | `knowledge/labarchives/<safe_page_name>.md` — just Read the file |
| Rebuild/refresh the KG | `python -m lab_agent.cli.build_kb [--index] [--full]` |
| Fetch a page's figures | `python -m lab_agent.cli.fetch_page_images "<page>"` |
| Zoom/crop a figure | `python -m lab_agent.cli.view_figure "<path>" [--crop X0 Y0 X1 Y1] [--scale 2]` |
| Resolution regression check | `python -m lab_agent.cli.eval_qa` |
| Refresh LabArchives cookies | `python get_la_cookies.py` |
| Slack (post/upload/read/channels) | `python -m lab_agent.cli.slack <cmd>` — see the Slack section above |
| DR conditions | `python run_dr.py "YYYY-MM-DD" [--hours N]` |

## Known limitation
Web app page IDs (e.g. `11400322`) do **not** map to API tree_ids. Pass page titles or base64 tree_ids instead. There is therefore **no per-page deep link** — when telling someone where a report is, give the notebook URL printed by the upload command plus "$UPLOAD_FOLDER / \<page title\>". Never invent, shorten, or elide a URL: a fabricated GitHub link in a draft channel message 404'd for the whole lab.
