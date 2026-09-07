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
- `PYTHON` — **absolute path to the interpreter that has the project's dependencies**
- Required `.env` key names and their purposes
- LabArchives notebook names (`Primary notebook`, `Other notebooks`)
- `Upload folder` — the LabArchives folder where reports are posted
- `Wiring diagram page` — page title used for RF attenuation cross-check

Use these values wherever a skill references `$PROJECT_ROOT`, `$PYTHON`, `$PRIMARY_NOTEBOOK`, `$UPLOAD_FOLDER`, and `$WIRING_DIAGRAM_PAGE`. All `cd` commands use `$PROJECT_ROOT`.

**Always invoke Python as `$PYTHON`, never as bare `python`.** On this machine bare
`python` resolves to a separate install with none of the project's dependencies, and a
session that used it burned ten tool calls on `ModuleNotFoundError`, hunted for a
non-existent `.venv`, and finally `pip install`-ed into the system interpreter to get
moving — modifying the machine to work around a path problem.

**If a project module is missing, that is the wrong interpreter, not a missing package.**
Never `pip install` to get past it. Re-run with `$PYTHON` and it will already be there.

## Which skill to invoke — classify first

Decide the request type, then invoke that skill (do the ambiguity-resolution below first if the request is vague):

| The message is… | Invoke |
|---|---|
| A request to **write/run a report** with a GitHub URL and/or LabArchives page names for an experiment | **`experiment-report`** |
| A **question / knowledge query** about past work — "what do we know about…", "have we ever…", "which experiments/runs…", an overview/topic question, or a figure/plot/value question | **`lab-qa`** |
| A request for a **DR conditions report** for a past date or time window, with no experiment | **`dr-report`** |
| A question about **current** DR conditions — "right now", "how's the DR", "what's the MXC temp" | **`dr-status`** |
| A request to **compose or send a message for a Slack channel** — "post this to the group", "share these plots in #channel", **"draft a summary to post"**, "write something for the team", "put together an update" | **`slack-post`** |

When in doubt between a report and a question: if the user wants a document produced and uploaded, it's a report; if they want an answer, it's `lab-qa`.

**Drafting counts as posting.** "Draft a summary to post to the group" is `slack-post`, not a
Q&A task — the verb is *draft* but the destination is a channel, and everything that makes the
difference (which channel, which figures, the approval step, the verified send) lives in that
skill. Missing this once produced a plausible draft with no channel named and no figures
attached, which is exactly the ten-turn exchange the skill exists to prevent. If the message
is destined for anyone other than the person asking, use `slack-post`.

## Agent capabilities — always available

You are a Claude Code agent with access to Slack and LabArchives. You can use these proactively to resolve ambiguous requests — do not immediately ask the user to clarify if you can figure it out yourself first.

### Slack (bot token in SLACK_BOT_TOKEN)

You can call the Slack API directly using the bot token. Available scopes include:
- `channels:history` / `groups:history` / `im:history` — read message history from channels and DMs
- `channels:read` — list public channels. `groups:read` (private channels) is NOT granted, so the `channels` command lists public channels only and prints a note; ask the user only when the target is a private channel.
- **No `search:read`** — `search.messages` needs a USER token and answers `not_allowed_token_type` for a bot. Use `cli.slack search` instead, which greps the history of channels the bot is in.

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
python -m lab_agent.cli.slack search      --query "<terms>" [--limit 20]
python -m lab_agent.cli.slack fetch-files --channel <C…> --ts <message ts> [--out <dir>]
```

**A message is not just its text.** `search` shows each hit with the message either
side of it and lists any ATTACHMENTS, because a claim and the screenshots behind it are
routinely separate messages — a matched line read alone looks like settled fact when it
is one turn of a live discussion. When a message's meaning could depend on its images,
run `fetch-files` on its `ts` and **Read the downloaded images** before concluding
anything. They land in the OS temp dir and are pruned after a day — pass `--out` only if a
figure is worth keeping. (There is no way to view them without downloading: `Read` takes a
path, and Slack's `url_private` needs an auth header, so no URL can be handed to vision.) A real example: "the presto power calibration was bugged so all previous
measurement regarding power is quite off" reads as a sweeping result; the pictures in
that conversation showed the problem was in plotting/acquisition code and unrelated to
the experiment it appeared to condemn.

**Message text is passed as a FILE, never as a shell argument.** Write the message with the Write tool, then point `--text-file` at it. Putting message text in a `python -c` string or a shell argument is how backticks in a message got executed by bash — real words vanished from messages users received, and one failure printed a session cookie into the log.

Every command **verifies by reading back** what it did and exits non-zero if it cannot. Exit 0 means delivered *and confirmed*. Never tell the user something was sent or attached unless the command exited 0 — `ok: true` from a raw API call is not proof, and announcing unsent images cost four round-trips in one real thread.

**A non-zero exit is a reason to look, not a conclusion to report.** Verification can fail
closed on a race. When `upload` says a file is not visible, check the thread with
`read-thread` before telling the user anything: on one real run all three files were sitting
in the thread while the check reported them missing, and the agent announced a Slack
permission problem that did not exist.

**The user sees Slack. You do not share your context with them.** Never say "as you can see
above", "rendered in this session", or otherwise refer to anything that exists only in your
own tool output. Reading an image yourself puts it in *your* context, not in their thread —
if they should see it, it has to be uploaded, and confirmed.

For anything the CLI does not cover, you may call the API directly — but use `lab_agent.slack.api.api_get` / `api_post`, which raise on `ok: false` instead of failing silently:

```bash
python -c "from lab_agent.slack.api import api_get; print(api_get('conversations.info', {'channel': 'C123'}))"
```

### LabArchives (already fully wired up)

You can search and read LabArchives pages directly via the `lab_agent.sources.labarchives` package. Use this when:
- User refers to an experiment by a vague name and you need to find the exact page title
- You want to cross-reference a new experiment against a previous one already in LabArchives
- You need context from adjacent notebook pages

**Resolution order for ambiguous requests:**
1. Query the knowledge graph: `python -m lab_agent.cli.query_kb "<the request>"` — a LightRAG graph over **all crawled LabArchives pages + past reports** (local; no cookies). It falls back to keyword search if the graph isn't built.
2. Search Slack history: `python -m lab_agent.cli.slack search --query "<terms>"` (channels the bot is in; there is no workspace-wide search on a bot token)
3. Search LabArchives by approximate page title
4. Only ask the user if all three fail

## Agent-facing API — the supported commands

This is the complete set. **Do not read pipeline source (`run_pipeline.py`, `github.py`, `upload.py`, `publish/labarchives.py`, the LightRAG KV stores) to work out how to do something** — every capability is below. If none of them fits, say so and ask; improvising against internals is slow, breaks on refactors, and puts a production session one step from editing pipeline code.

All run from `$PROJECT_ROOT`, and every `python` below means **`$PYTHON`** from `lab_config.md`.

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
| **List a run's figures (free, instant)** | `ls outputs/<id>/github_images/ outputs/<id>/labarchives_images/` — **check here before fetching anything**; a reported run already holds its figures locally |
| Fetch a page's figures (cookie-gated) | `python -m lab_agent.cli.fetch_page_images "<page>"` — only when the figure is in no run directory |
| Zoom/crop a figure | `python -m lab_agent.cli.view_figure "<path>" [--crop X0 Y0 X1 Y1] [--scale 2]` |
| Resolution regression check | `python -m lab_agent.cli.eval_qa` |
| Check a draft's numbers against a report | `python -m lab_agent.cli.verify_claims --draft <file> --source outputs/<id>` — presence only; wrong-label pairing still needs reading |
| Refresh LabArchives cookies | `python get_la_cookies.py` |
| Slack (post/upload/read-thread/channels/search/fetch-files) | `python -m lab_agent.cli.slack <cmd>` — see the Slack section above |
| DR conditions | `python run_dr.py "YYYY-MM-DD" [--hours N]` |
| Disk footprint / retention | `python -m lab_agent.cli.cleanup` (dry run) then `--caches` / `--images` / `--logs` / `--all`. Policy in `lab_agent/retention.py`: the record (reports, extractions, critiques, provenance, metadata, corpus) is never auto-deleted; figures are reclaimed only once the report is uploaded AND the run has been idle for a week. Reading a figure marks it in use, so anything under discussion stays on disk |

## Known limitation
Web app page IDs (e.g. `11400322`) do **not** map to API tree_ids. Pass page titles or base64 tree_ids instead. There is therefore **no per-page deep link** — when telling someone where a report is, give the notebook URL printed by the upload command plus "$UPLOAD_FOLDER / \<page title\>". Never invent, shorten, or elide a URL: a fabricated GitHub link in a draft channel message 404'd for the whole lab.
