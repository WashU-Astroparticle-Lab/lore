# Live test plan — Stages A–E

Everything in Stages A–E is unit-tested and two changes were exercised against real
subagents, but the *interaction* fixes (intake, thread read-back, file-upload wake,
summary posting, page reuse) can only be verified by a real Slack session. This is the
script for that.

Each step says what to send, what should happen, and **what the old behaviour was** —
so a pass is falsifiable rather than a vibe.

## Before you start

1. **Re-authenticate the CLI:** run `claude` in a terminal. The knowledge graph's LLM
   runs on that login; while it is expired `query_kb` returns nothing (it now says so
   loudly instead of returning the auth error as an answer).
2. **Restart the listener.** `sessions.py` and `listener.py` both changed, and the spawn
   prompt is baked into the running process — CLAUDE.md and the skills are re-read per
   session, but the prompt is not.
3. **Only one listener.** Socket Mode load-balances across every connected listener, so
   if the lab machine's is up it will steal roughly half the messages and run old code.
   Stop it first.
4. Optional: `python -m lab_agent.cli.cleanup` to see the disk footprint before/after.

---

## Phase 1 — no writes to LabArchives (~10 min)

Safe to run any time; nothing reaches the notebook.

### 1.1 File-upload wake  *(D4)*
**Send:** a photo in a DM with **no text at all**.
**Expect:** LORE responds.
**Was:** silence. A file-only message arrives with `subtype: file_share` and the handler
dropped every subtype, so on Aug 26 you uploaded 51 s after being told "just drop it in
the thread" and waited 6 min 05 s before nudging.

### 1.2 Q&A routing  *(E1)*
**Send:** `Could you give a summary on 20260702 JPL QPDs?`
**Expect:** an answer within a few seconds, drawn from `knowledge/labarchives/20260702_JPL_QPDs.md`.
**Was:** 69 s in `query_kb` for a thin result, then answered from thread context anyway.

### 1.3 Channel-message drafting  *(D5, C1)*
**Send:** `Could you draft a short summary of the Aug 31 no-filter run to post to the group?`
**Expect:** **one** complete draft that already includes the channel list (`#data_analysis`
among them) — and no send until you approve.
**Was:** ten turns and 26 minutes, including "which channel?" asked four times while the
list was one command away.

---

## Phase 2 — full report (creates LabArchives pages)

Run when you are willing to have new pages in the AI Agent folder.

### 2.1 Intake, and no DR question for bench work  *(D1)*
**Send:** a report request for a bench experiment (a GitHub URL, no fridge involved).
**Expect:** three intake questions — *what question was this answering / DR? / full or
brief?* — **with the DR question omitted** because it is bench work. Fetching starts
immediately without waiting for answers.
**Was:** the DR question asked first and unconditionally; on Aug 26 it stalled the
pipeline 12 minutes at the Phase A/B boundary, and the objective was never asked at all
(you supplied it at turn 9, after two rewrites).

### 2.2 Mid-run instruction  *(D3)*
**While Phase A is running, send:** `only use the two NoFilter plots`.
**Expect:** a later post that explicitly acknowledges it.
**Was:** silently dropped. "you only need the two plots from the No filter ipynb" appears
in zero of that session's transcript — the listener absorbed it — while the next post
said "Writing report…".

### 2.3 Directory collision  *(A2)*
**Use the real trap:** ask for a report on an experiment whose notebook lives in a
*previous* run's GitHub folder.
**Expect:** either LORE passes `--experiment-id`, or `run.py` exits 4 and it retries with
a fresh id. Check that the earlier run's `metadata.json` is untouched.
**Was:** the Aug 31 run overwrote `outputs/presto_vna_spectrum_20260826/metadata.json`,
which now cites the Aug 31 page and commit — that experiment's provenance is gone.

### 2.4 Brief template + gated summary  *(B, A1)*
**Expect:** ~80–120 lines, headings that state findings, no ToC / Key Parameters /
Methods / Citations, and a Slack summary whose numbers and hedging match the report.
Watch specifically for **"confirmed"/"confirms"** in the Slack message — it should not
appear unless the report earns it.
**Was:** the Aug 31 summary said "agreement confirmed" and "confirming asymmetric
insertion loss" minutes after the critic stripped "confirming" from the report; the
Aug 26 summary swapped the 6.9 / 6.44 GHz offsets.

### 2.5 Revision + page reuse  *(A4, A3)*
**Send:** `make it shorter and drop the key parameters table`.
**Expect:** progress posts showing report-writer **and** the critic re-running, then an
upload that adds a *revision entry to the existing page*. Confirm no new duplicate
appears (there are already four pages titled `[UNSIGNED] presto_vna_spectrum_20260826`).
Also check it cites the LabArchives location rather than inventing a URL.
**Was:** the orchestrator rewrote the report itself from the `[UNSIGNED]` draft, skipping
the critic entirely — the Aug 26 report in the notebook was never critiqued — and every
upload created a new page.

---

## Recording the result

For each step: pass / fail / not-reached, and paste anything surprising. A failure here is
more valuable than a pass — the unit tests already cover the deterministic layer, so what
this exercises is precisely the part that could not be tested any other way.

Known limits going in:
- The critic is non-deterministic (items 2 and 9 were flaky across three runs on identical
  content), so a single clean critique is not proof the gate always fires.
- Phase 2 costs plan usage and ~15–25 min per report.
