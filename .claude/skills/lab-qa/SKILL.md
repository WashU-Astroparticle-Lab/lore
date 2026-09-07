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

0. **Does the question name a page? Then read that page, don't ask the graph.**
   For "summarise \<page\>", "what does \<page\> say about X", or any question naming one page, read the crawled copy directly:

   ```bash
   ls knowledge/labarchives/ | grep -i "<part of the page name>"
   ```

   then Read the matching `knowledge/labarchives/<safe_page_name>.md` — the full page text, already on disk, **free and instant**. Answer from it.

   The graph is built for questions that span pages; on a single named page it is both slower and worse. Asked to summarise `20260702 JPL QPDs`, `query_kb` spent 69 s and came back thin — while the entire page text sat in the corpus unread. Reserve steps 1–2 for genuinely cross-page questions ("which chips have we measured", "have we ever seen X"), where the graph is strong.

   If no corpus file matches the name, fall through to step 1.

1. Query the knowledge graph: `python -m lab_agent.cli.query_kb "<the question>"`. It answers over the full crawled LabArchives corpus + past reports (LightRAG graph — falls back to keyword search if the graph isn't built), returning an answer with experiment/page **citations**.
2. **Gauge coverage before answering.** If `query_kb`'s output clearly and specifically addresses what was asked, reply from it, **citing the source(s)**; **never fabricate numbers** — state only what the graph returns. If the output is thin, generic, or doesn't address the specifics, treat it as low-confidence and go to step 4 instead of stretching it into an answer.
3. **Do NOT** run `run.py`, **do NOT** open/fetch LabArchives pages, and **NEVER** run `get_la_cookies.py` for a text-only knowledge question.
4. **If the question hinges on an identifier or vague reference** the graph couldn't resolve (chip/run/sample ID, "the KID sweep from last Tuesday") → use the **find-device-notes** skill to resolve the page and surface its notes, then answer from that. Don't stop to ask the user for a page name an identifier hit already found.
5. **If the message is about a specific figure / plot / value → use the deduce-figure skill.** That covers both kinds: *interpretation* ("what value does that graph show?", "the mean Qc", "is there a resonance dip?") **and "show me" requests** ("can you show me the two no-filter plots?", "send me the Qc scatter", "what do those look like?"). A "show me" is not a file-delivery errand — the reply the lab wants is the figures **plus** a couple of lines saying what they show. Handling it ad hoc here skips deduce-figure's rules and produced exactly that: one session found the right two PNGs by filename, uploaded them, and answered "Both plots are uploaded and confirmed in the thread" without ever opening them.
6. **Ambiguous or truly absent → clarify, don't guess.** If nothing (graph, candidates, or figures) matches, say so plainly and **offer** to run a full report (which does need cookies) — do not fabricate, silently start fetching, or trigger a cookie refresh.

(The graph is refreshed by `python -m lab_agent.cli.build_kb --index` on a schedule; new experiments also enter it at report time.)
