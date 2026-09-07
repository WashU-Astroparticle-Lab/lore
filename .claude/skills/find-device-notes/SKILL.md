---
name: find-device-notes
description: Resolve a device/chip/run/sample identifier (e.g. BE260416, JKID5x, WH2) or a vague experiment reference to its LabArchives page(s), and surface the notes on that page. Local, deterministic, free — no cookies, no fetching. Use whenever you need to map an identifier or fuzzy reference to a concrete page before reading or answering (lab-qa and the report pipeline both call on this).
---

# Find the LabArchives notes for a device / identifier

Given a device, chip, run, or sample ID — or a vague experiment reference — find which LabArchives page(s) it corresponds to and surface that page's notes. Read `lab_config.md` (per CLAUDE.md) for `$PROJECT_ROOT`. This path is fully local: **no cookies, no fetching.**

## Why this is its own step
The concept graph is weak at raw identifiers — the LLM rarely lifts a bare ID (`BE260416`) into a graph entity, and IDs often live only inside hyperlink filenames (`BE260416-NG-D1-CPB_qct_20260709.ipynb`). A deterministic identifier resolver maps the ID to its page far more reliably than graph or plain TF-IDF (which lets common words like "quantum capacitance" outrank the decisive rare ID).

## Steps

1. Run the resolver:
   ```bash
   cd $PROJECT_ROOT
   python -m lab_agent.cli.query_kb "<the identifier or reference>"
   ```
   Under its answer, `query_kb` prints a **"Candidate pages (identifier / keyword match)"** list — **identifier hits first** (pages whose text literally contains the ID, tagged `id×N`), then TF-IDF keyword hits (`kw <score>`). For a pure identifier lookup you can also run `python -m lab_agent.cli.ask "<identifier>"` directly.

2. **Pick the page:**
   - A clear top identifier hit (`id×1`+) that matches the request → **use it**. Don't stop to ask the user for a page name the identifier hit already found.
   - Several candidates match equally (genuinely ambiguous) → briefly list them and **ask which one**.
   - Nothing matches → say so plainly; offer a full report (which needs cookies) rather than fabricating.

3. **Surface the notes.** The resolved candidate is tagged `la_page:<safe_name>` — its crawled text lives locally at `knowledge/labarchives/<safe_name>.md`. Read that file to quote/summarise the notes (still no cookies). Only if the caller needs *figures* from the page, hand off to the **deduce-figure** skill (that step is cookie-gated).

4. Report the resolved page title + the relevant notes, **citing the page**. Never fabricate — state only what the page text contains.
