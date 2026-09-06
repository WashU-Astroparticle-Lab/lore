# LORE Interaction & Correctness Roadmap (Stages A–E)

Derived from a full-transcript review (2026-09-05) of two real Slack threads: the
Aug 26 + Aug 31 `presto_vna_spectrum` report thread (25 turns) and the Aug 17 Q&A
day (9 turns). Companion to `ROADMAP.md` (Stages 0–5 covered *capability*; this one
covers *usability and the correctness of what actually reaches the human*).

## The measuring stick

LORE's value = (time it saves) − (time spent steering it). Measured over the
reviewed threads:

| Work | User messages | Wall clock | Verdict |
|---|---|---|---|
| Aug 26 report | 10 | 2 h 09 | net win, heavy attention cost |
| Aug 31 report | 2 | 23 min | near-ideal |
| Simple version | 1 | 3 min | ideal |
| Channel message | 10 | 26 min | **net loss** |
| Q&A follow-ups (Aug 17) | 5 | 1–4 min each | 5/5 first time |

**The governing pattern:** LORE is first-time-right when the target is objective (a
number, a plot read, a chip list) and needs 3–5 rounds when the target is subjective
(what should this say, how long, for whom). The gap is not model capability — it is
that the subjective parameters are never stated. Every stage below either states
them up front or removes the need to.

---

## Stage 0 — Baseline (before any edit)

Everything since July is uncommitted on `main` (HEAD `8fa677a`). Editing further on
top of an uncommitted ~50-file delta means there is no rollback point.

1. Branch off `main` and commit the existing Stage 0–5 work as-is.
   `CLAUDE.md` and `.claude/skills/` **must** land in the same commit, or the lab
   machine gets a router with no skills.
2. Record the baseline: `for t in tests/test_*.py; do PYTHONPATH=. python "$t"; done`,
   plus `python -m lab_agent.cli.eval_qa`.
3. Only then start Stage A.

---

## Stage A — Stop the bleeding (correctness + data integrity)

Small, high-value, no redesign. Fixes the defects that reached LabArchives or Slack
unchallenged.

### A1. Gate the Slack summary

The report gets 9 critic items plus a structural gate; the Slack message the lab
actually reads gets nothing — and that is where both factual defects occurred (a
frequency/offset transposition on Aug 26; "confirmed"/"confirming" on Aug 31,
minutes after the critic stripped exactly that word from the report).

- `report-writer` emits `slack_summary.md` (≤6 bullets) alongside the report.
- New critic item: every number and claim in `slack_summary.md` appears in the
  report with the same value and the same hedging.
- The `experiment-report` skill posts that file **verbatim** rather than composing
  fresh prose from memory of subagent results.

### A2. Stop output directories colliding

`run_pipeline.py` overrides the GitHub-derived experiment id only when the URL was
*auto-discovered* (~line 258). An explicitly-given URL always wins — which is why
the Aug 31 run wrote into `outputs/presto_vna_spectrum_20260826/` and overwrote that
experiment's `metadata.json` (it now cites the Aug 31 page and commit).

- Add `--experiment-id` / `--out-dir` to `run.py`.
- Refuse to write into an existing out_dir whose `metadata.json` names a different
  `github_url` or `la_pages`, unless `--reuse` or `--force` is given.

### A3. Upload updates instead of duplicating, and returns its URL

`upload_report()` always creates a page. Four pages now share the title
`[UNSIGNED] presto_vna_spectrum_20260826`.

- Look up the page by title in the upload folder; add or replace the entry on the
  existing page when found.
- Add `--report-file`. Today the CLI takes only `<out_dir>` and picks the newest
  `[UNSIGNED]` by mtime — there is no way to upload a specific report.
- Print and return the LabArchives **web URL**, and store it in `metadata.json`.
  LORE could not link its own upload during the channel-message task and produced an
  elided GitHub URL instead, which 404'd.

### A4. Revisions must re-enter the pipeline

On Aug 26 the orchestrator revised the report itself with `Write`, using the
`[UNSIGNED]` draft as its source — which `docs/report_style_guide.md:85` and
`.claude/agents/report-writer.md:11` both forbid. The rule binds the subagent, and
revisions bypass the subagent. `critique.md` is timestamped 16:25; the report in
LabArchives is 17:44. **The uploaded version was never critiqued.**

- All revisions go through `report-writer` in revision mode, then re-run the critic.
- Revision rebuilds from the extracted files plus the user's instruction — never
  from the previous draft.
- Only single-token/typo fixes may be applied inline (as on Aug 31), and must be
  logged in `critique.md`.

---

## Stage B — The simple template becomes the default

User decision (2026-09-05): the plain-language version was the best output LORE
produced and should be the default. Exemplar:
`outputs/presto_vna_spectrum_20260831/[UNSIGNED] presto_vna_spectrum_20260831_simple.md`

This is not a prompt swap — the gate and the critic both hang off the old scaffolding.

### B1. New default structure in `docs/report_style_guide.md`

What We Did → setup → Main Result → New Finding(s) → what it means → Open Questions
→ Sources. Section headers state the *finding*, not the category. Each figure sits at
the claim it supports, with a caption saying what to look at. A bold **Bottom line:**
per block. Plain language, exact numbers retained, honest uncertainty preserved. No
ToC, no Key Parameters table, no Methods section, no Citations block.

### B2. `lab_agent/cli/eval.py` — stop requiring Key Parameters

Line ~164 raises `[ERROR] no Key Parameters section`. This fired twice against an
explicit instruction to drop the table, and LORE silently bolted a minimal one back
in both times. Replace with checks that fit the new template: title present, every
referenced image path exists on disk, Sources section present, sidecar present.

### B3. `.claude/agents/critic.md` — re-anchor items 1 and 9

Item 1 checks the Key Parameters `Source` column; item 9 checks `[n]` citation
markers. Both disappear with the new template, and sourcing discipline must not
disappear with them.

- `report-writer` emits a sidecar `report_sources.md`:
  `| value or claim | where in report | extracted file | exact source line |`
- Item 1 becomes: every numeric value in the report body appears in the sidecar and
  matches the cited extracted file.
- Item 9 becomes: every sidecar entry resolves.

Net effect: the *reader* gets a clean document, the *auditor* keeps a complete
claim→source map. This is stronger than today, not a relaxation.

### B4. Keep the detailed template behind an explicit request

"Full report" / `--full`, not deleted.

### B5. Re-baseline `tests/golden/` against the new template.

---

## Stage C — Kill the improvisation

The largest time sink across both threads. Every session hand-rolls the same API
calls in `python -c` and gets them wrong differently each time.

Observed consequences: backticks in messages interpreted by bash (the user received
"with  scope, update it in the  file" — `repo` and `.env` were eaten); one failure
executed `./.env` and echoed the LabArchives session cookie into tool output;
repeated Windows-path `SyntaxError`s; a 6-attempt `invalid_arguments` loop just to
read thread replies; the deprecated `files.upload`; **four turns and two user
complaints to attach two images**; and posts that printed nothing, leaving success
unverifiable.

### C1. `python -m lab_agent.cli.slack` — one tested entry point

`slack/api.py` already has `post_message()` and `slack_get()`. Wrap and extend them:

- `post --channel --thread --text-file <path>` — always a file, never a
  shell-quoted string
- `upload --channel --thread --file <path> [--comment ...]` — correct three-step
  `files.getUploadURLExternal` → PUT → `completeUploadExternal`
- `read-thread --channel --thread`
- `channels` — list channels the bot can post to (LORE asked "which channel?" four
  times without ever offering the list it can read)
- **Every command verifies by read-back before reporting success.** LORE announced
  "Both plots are in your thread now!" when they were not, trusting an `ok: True`
  from an incomplete flow.

### C2. `check_env --live`

`config.check_env()` prints `set`/`MISSING` and never validity. The Aug 26 GitHub
token loop cost **75 minutes across 6 turns** because "set" was reported while the
token returned 401. Add live probes: GitHub `/user` plus target-repo reachability,
LA HMAC, LA cookie, Slack `auth.test`. Print `valid` / `invalid: <reason>` /
`MISSING` only — never values, lengths, or prefixes. (LORE printed token prefixes
repeatedly and posted one into Slack.)

### C3. Skills forbid ad-hoc Slack scripting

All four skills call the CLI. No `python -c` for Slack, ever.

### C4. Document the agent-facing API

Both threads show LORE reverse-engineering its own codebase live —
`inspect.signature()`, `dir()` on modules, opening LightRAG's
`kv_store_full_docs.json` raw and discovering by `KeyError` that the value was a
dict; reading `github.py`, `run_pipeline.py`, `upload.py`, `publish/labarchives.py`.
The root cause is always a missing CLI verb. A short "agent-facing API" section in
`CLAUDE.md` listing every supported command closes this, and keeps a production
session out of pipeline source.

---

## Stage D — The interaction contract

### D1. Replace the DR question with a real intake

`experiment-report` Step 0 asks about dilution-refrigerator conditions before
anything else. It was irrelevant in both runs of the reviewed thread, and on Aug 26
it stalled the pipeline for 12 minutes at the Phase A/B boundary. Meanwhile the one
question that would have prevented two revision rounds was never asked — the user
had to volunteer the experiment's actual objective at turn 9.

New Step 0: one post, non-blocking, answerable in a line.

1. What question was this experiment trying to answer?
2. DR conditions needed? (skip the ask entirely for bench / room-temperature work)
3. Full report or brief?

### D2. Preferences persist

"Shorter", "no Key Parameters", "dBm only" were established on Aug 26 and lost by
Aug 31. Write durable style preferences to `knowledge/preferences/<user>.md` when a
revision expresses one; `report-writer` reads it. Stage B makes the most common
preference the default, so this covers the remainder.

### D3. Never silently drop a mid-run message

"you only need the two plots from the No filter ipynb" never reached the running
session — zero occurrences in that session's transcript; it appears only in later
sessions' inert history. LORE posted "Writing report..." immediately afterwards, so
it looked received. Queue replies and deliver them at the next phase boundary; if
that is impossible, reply "noted — I'll apply that at the revision step". Silence is
the one unacceptable option.

### D4. Wake on file uploads

"Just drop it in the thread and I'll kick off the pipeline" is a promise the listener
cannot keep — a file upload with empty text produces no event it reacts to. On Aug 26
the user uploaded 51 s after asking and waited **6 min 05 s** before nudging; the same
happened again on Aug 31. Subscribe to `file_shared` / messages-with-files in owned
threads.

### D5. A `slack-post` skill

Owns draft → approve → send-with-images, and offers the channel list up front. The
reviewed exchange took 10 turns and 26 minutes to send a five-line message with two
plots; this should be three turns.

---

## Stage E — Q&A routing and cost

### E1. Route named-page questions around the graph

"Summarize page 20260702 JPL QPDs" — `query_kb` spent 69 s and returned little, and
LORE answered from thread context instead. But "which QPD chips have we measured"
produced a good cross-campaign table in 56 s. The graph is strong on cross-page
questions and weak on single-page summaries — backwards from what people ask most.
If the question names a page, read `knowledge/labarchives/<page>.md` directly:
already on disk, free, instant. Reserve the graph for genuinely cross-page questions.

### E2. Cookie lifetime

The number-one hard blocker: it killed two of the four attempts at the Qc question,
needed two refreshes inside 30 minutes on Aug 31, and appears in 7 of 109 session
logs. Probe at session start when the request is likely to need figures, and refresh
before the work rather than midway through it.

---

---

## Back-check against the current code (2026-09-05)

The reviewed threads ran on a slightly older LORE, so every finding was re-verified
against the working tree before this plan was accepted.

### Already fixed — do not re-do

| Finding | Evidence it is fixed |
|---|---|
| Chip-ID → page resolution (the 4x re-ask of the Qc question) | `candidate_pages` present in `cli/ask.py` and `cli/query_kb.py`; the fix landed later on Aug 17, after those sessions |
| Cookie self-refresh (Q&A had to ask the user to run `get_la_cookies.py`) | now in `deduce-figure/SKILL.md:21` and `experiment-report/SKILL.md:47`; self-refreshed twice unattended on Aug 31 |
| Slack file download blocked by missing `files:read` | scope granted; worked on Aug 31 |
| Resume-failure `exit 1` | auto-fallback to a fresh session; no occurrences in the Aug 26/31 runs |
| Skills router unproven in production | Aug 31 t11 invoked `Skill: experiment-report` on the live Slack path |

Residual: the cookie *lifetime* is still short (two refreshes inside 30 minutes on
Aug 31) — that part of E2 stands.

### Still live — verified in the working tree

| Stage | Check | Result |
|---|---|---|
| A1 | `grep -rn slack_summary` | zero hits anywhere; the skill still says only "write your final summary as normal text output" |
| A2 | `run_pipeline.main()` | positional args only — no `--experiment-id`, no collision guard |
| A3 | `publish/labarchives.py:317` | docstring step 2 is still "Create a new page named *page_title*" |
| A4 | `experiment-report/SKILL.md:133` | **narrower than first written** — the report-writer/re-critique loop IS documented, but only under Phase D for critic FAILs. There is no path for a *user-requested* revision after upload, which is the case that improvised on Aug 26. Fix = add that path, not rebuild the loop. |
| B2 | `cli/eval.py:164` | still `err("no Key Parameters section")` |
| C1 | `lab_agent/cli/` | no slack module |
| C2 | `config.check_env()` | prints `set`/`MISSING`, no validity probe |
| D1 | `experiment-report/SKILL.md` Step 0 | DR question still first, still unconditional |
| D3 | `slack/sessions.py:~564` | the listener tells the user "the reply will be picked up by the running session", but the session only polls at the DR checkpoint. Everything else is lost — a promise the code makes explicitly and does not keep. |
| D4 | `slack/listener.py:71` | `if event.get("bot_id") or event.get("subtype"): return` — a file upload arrives with `subtype: file_share` and is dropped. **This one line is the entire cause of the 6-minute dead air, in both threads.** |
| E1 | `lab-qa/SKILL.md:16` | step 1 sends every question to `query_kb`; no named-page shortcut |

**Why the already-fixed set does not undercut the plan:** every one of those fixes was
about *finding things and authenticating* — resolution, cookies, scopes, resume. None of
them touched what happens to information once found: how it is summarised for the human,
how revisions are handled, where output lands, or how many turns a task costs. That is
exactly the surface Stages A–E address, and it is untouched.

## Suggested order

Stage 0 → A → B → C → D → E.

A and B are the correctness and output-quality core and are largely independent of
each other. C is the biggest single win in *researcher* time and can proceed in
parallel with B. D depends on C1 (the Slack CLI). E is independent and cheap.

Regression net for every stage: the existing 18 test files, `eval check`, and
`eval_qa` (6/6) — run all three before and after.
