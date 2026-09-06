---
name: lab-qa
description: Answer a knowledge/overview question about the lab's past work from the local knowledge graph — "what do we know about…", "have we ever…", "which experiments/runs…", topic/overview questions, or requests to find/summarise past findings, including questions about a specific figure/plot/value. Local and fast — no LabArchives cookies and no fetching for text questions. Use this instead of generating a report when the message is a question rather than a request to write/upload a report.
---

# Answering questions (fast-path — no report pipeline, no cookies)

This is the entry point for lab knowledge questions. Read `lab_config.md` (per CLAUDE.md) for `$PROJECT_ROOT`. Text questions are local — no cookies, no fetching. Keep replies conversational and concise — this is a Slack reply, not a report.

For two sub-capabilities this skill delegates (single source of truth — don't reinvent them here):
- **Mapping a device/chip/run ID or a vague reference to a page + its notes → use the `find-device-notes` skill.**
- **Reading/interpreting a specific figure/plot to get a value → use the `deduce-figure` skill.**

## Steps

1. Query the knowledge graph: `python -m lab_agent.cli.query_kb "<the question>"`. It answers over the full crawled LabArchives corpus + past reports (LightRAG graph — falls back to keyword search if the graph isn't built), returning an answer with experiment/page **citations**.
2. **Gauge coverage before answering.** If `query_kb`'s output clearly and specifically addresses what was asked, reply from it, **citing the source(s)**; **never fabricate numbers** — state only what the graph returns. If the output is thin, generic, or doesn't address the specifics, treat it as low-confidence and go to step 4 instead of stretching it into an answer.
3. **Do NOT** run `run.py`, **do NOT** open/fetch LabArchives pages, and **NEVER** run `get_la_cookies.py` for a text-only knowledge question.
4. **If the question hinges on an identifier or vague reference** the graph couldn't resolve (chip/run/sample ID, "the KID sweep from last Tuesday") → use the **find-device-notes** skill to resolve the page and surface its notes, then answer from that. Don't stop to ask the user for a page name an identifier hit already found.
5. **If the question is about a specific figure / plot / value** ("what value does that graph show?", "the mean Qc", "is there a resonance dip?") → once you have the page (via step 4 / find-device-notes if needed), use the **deduce-figure** skill to fetch and read it.
6. **Ambiguous or truly absent → clarify, don't guess.** If nothing (graph, candidates, or figures) matches, say so plainly and **offer** to run a full report (which does need cookies) — do not fabricate, silently start fetching, or trigger a cookie refresh.

(The graph is refreshed by `python -m lab_agent.cli.build_kb --index` on a schedule; new experiments also enter it at report time.)
