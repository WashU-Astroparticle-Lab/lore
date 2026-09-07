---
name: deduce-figure
description: Answer a question about a specific figure/plot/graph on a LabArchives page — "what value does that plot show?", "is there a resonance dip?", "what's the mean Qc?" — by fetching the page's figures and reading/interpreting them. Cheap-by-default (read inline), precise-when-needed (delegate to an Opus image-analyst with view_figure zoom). Cookie-gated; self-refreshes. Needs a page name — if you only have an identifier/vague reference, resolve it first with find-device-notes.
---

# Deduce the answer from a figure

Fetch a LabArchives page's figures and read/interpret a specific plot to answer the question. Read `lab_config.md` (per CLAUDE.md) for `$PROJECT_ROOT`. **This is the one figure path that needs the LabArchives cookie, and you handle it yourself.**

If you only have an identifier or vague reference (not a page name), resolve it first via the **find-device-notes** skill, then come here with the page name.

## Step 0 — look in the run directory FIRST (local, free, instant)

If the figure could belong to an experiment that has been reported, its files are already
on disk. Check before fetching anything:

```bash
ls outputs/<experiment_id>/github_images/ outputs/<experiment_id>/labarchives_images/
```

Not sure of the id? `ls outputs/` — the names are experiment ids. A run directory holds
both the GitHub notebook figures **and** the LabArchives page images that run used, with
meaningful filenames (`Presto_vs_VNA_NoFilter_cell4_out1.png` names notebook, cell and
output). No cookie, no download, no wait.

**This is the cheapest source and it was being checked last.** One real request for "the
Presto vs VNA no-filter comparison plots" went `query_kb` → fetched 18 unrelated
LabArchives figures → spawned a subagent → waited 150 s → and only then ran `ls outputs/`,
where both files had been sitting the whole time. 275 seconds for 25 seconds of work.

Only go to Step 1 when the figure is **not** in any run directory — a page that has never
been through the report pipeline.

## Step 1 — fetch the figures (only if Step 0 found nothing)

```bash
cd $PROJECT_ROOT
python -m lab_agent.cli.fetch_page_images "<page name>"
```

Downloads the page's figures to a local cache and prints their paths, plus a `sources.json` holding each figure's LabArchives id.

## Step 1b — identify the figure by CONTEXT, never by its number

`fig_1`, `img_5` are **positions on the page today**, not identities. Insert one image at the top of a LabArchives page and every later number shifts — so "the report's fig_3" or "the fifth image" is not a durable way to name a figure, and matching on it will eventually point at the wrong plot.

Identify by what the figure *is*, using everything available, cheapest first:

1. **`sources.json` → `la_id`.** LabArchives assigns each inline image its own id (e.g. `1783040576203.png`), stable across reordering. If a prior report, `provenance.md` or `metadata.json` records an id, match on that and you are done.
2. **The filename, when it carries meaning.** GitHub figures are named from their origin — `Presto_vs_VNA_NoFilter_cell4_out1.png` says notebook, cell and output. That survives everything. LabArchives names (`..._img_1.png`) do not; they are positional.
3. **The surrounding page text.** The crawled page (`knowledge/labarchives/<page>.md`) describes what was measured near each figure, and a prior run's `extracted_labarchives.md` Figures section describes each one directly. A description is a far better handle than an index.
4. **What the user actually said.** "the RF-Out-OFF screenshot", "the Qc vs Qi scatter", "the one with the double peak" — that is usually enough to pick the figure outright.
5. **Look at the candidates.** If context still leaves two or three possibilities, **Read them** and choose the one that matches. This is cheap and decisive — a glance settles what an index never could. Then state which figure you picked and why.

Only ask the user which figure they mean once 1–5 have genuinely failed. And when you cite a figure, cite something durable: its `la_id`, its meaningful filename, or a description of its content — not just "fig_3".

**If it prints `COOKIE_REFRESH_NEEDED`:** run `python get_la_cookies.py` yourself (a figure question genuinely needs the cookie — this overrides the text-only "never refresh" rule). First post a short Slack heads-up (*"Refreshing the LabArchives session — please approve the Duo push on your phone"*), then run it and retry `fetch_page_images`. The script auto-skips if the cookie is still valid and auto-fills credentials; the only human step is the Duo tap (and only when off school wifi). Only if refresh fails/times out, tell the user.

## Step 1c — if the run has a report, its numbers beat your read of the pixels

Describing what a figure shows is valuable and wanted. Reading *numbers* off it when an
exact value already exists is not — a visual read drifts, and it has drifted every time:
"≤0.1 dB" where the report said 0.15, "~0.3 dB" where it said 0.35, and "~0.55 dB at
−34 dBm" where both the report and the plot's own title say **0.35**. That last one is
simply wrong, and 0.55 appears nowhere in the run.

So when the figure comes from `outputs/<id>/`:

1. Read the figure and describe it in your own words — that part stays.
2. **Take every number from the report** (`[UNSIGNED] *.md`) or `provenance.md`, not from
   your read of the image.
3. If what you see genuinely disagrees with the report, **say so explicitly** — that is a
   real finding worth surfacing, not something to quietly average away.

A figure with no report behind it is the only case where your read is the source, and there
you give an honest ± rather than a bare number.

## Step 2 — read the figure: cheap-by-default, precise-when-needed

Accuracy on plots matters, but only pay the heavy read when the question needs it.

- **Qualitative / identification** ("which plot is this?", "what does this graph show?", "is there a resonance dip?" — a glance at one or a few figures): Read the returned image paths yourself and answer, **citing which figure** (page + fig number). Stay on your default model. If a figure is flagged oversized, first run `python -m lab_agent.cli.view_figure "<path>"` and Read the path it prints (raw >2000 px figures are rejected by the vision API).

- **Precise / quantitative reads** ("what *exact* value…", a mean, reading many points off a plot), **OR** many figures to sift through, **OR** low-confidence, **OR** user pushback ("look closer", "that's not right"): delegate the WHOLE figure job to **one Opus image-analyst subagent** (Agent tool, `general-purpose`, `model: "opus"`) with a fresh context — strongest vision model, and it avoids the many-image saturation that stalls a long main session. Pass it the fetched image directory + the exact question, and instruct it to:
  1. **Survey** the cached figures to find the one(s) holding the answer (use `python -m lab_agent.cli.view_figure "<path>"` for any oversized figure).
  2. **Zoom** the answer figure if the detail is small — `python -m lab_agent.cli.view_figure "<path>" --crop X0 Y0 X1 Y1 --scale 2` (coords are 0–1 fractions of width/height) — and Read the crop for a legible read.
  3. Read each value carefully, **compute** the requested statistic, and report **per-item values + the result + which figure + an honest confidence/± range**. Never invent values it cannot resolve; if the figure doesn't permit a reliable read, say so and point to the source (e.g. the linked analysis notebook).
  Token-efficient: one subagent, a cheap survey, high-res zoom only on the figure that matters.

  **Spawn it BLOCKING — `run_in_background: false`.** A Slack session is one-shot: it does
  the work and exits. An async agent that promises "you will be notified when it completes"
  has nobody left to notify, and its answer is lost. One run launched one, waited 150 s,
  gave up, did the search itself and ignored the result — 275 seconds for 25 seconds of
  work — and that agent's output file was still **0 bytes** three minutes after the session
  ended. It never returned anything at all.

  **Delegate or do it — never both.** If you spawn an analyst, use what it returns. If the
  result is empty or does not arrive, say so in your reply rather than silently substituting
  your own answer; a delegated read that produced nothing is worth the user knowing about.

  **Only delegate when the read is genuinely hard** — reading many points off a plot,
  computing a statistic, resolving fine detail. Opus earns its cost there: it once read nine
  scatter points off a Qc-vs-Qi plot and returned a mean of ~63,000 ± 3,000 that matched a
  careful manual read. For "which plot is this" or "what does this show", Step 0 plus your
  own read is faster and just as good.

- If several figures could match, briefly describe the candidates and **ask which one** before going deep — don't assume.
