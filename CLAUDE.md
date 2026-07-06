# LORE — Pipeline Instructions

When the user gives you a GitHub URL and LabArchives page names, run the full pipeline and write the report (Steps 1–4 below).

If the user asks for a **DR-only conditions report** (no GitHub URL, just a date or time window), skip directly to the DR-Only Report workflow at the bottom of this file.

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

You can search and read LabArchives pages directly via `lab_agent/sources/labarchives.py`. Use this when:
- User refers to an experiment by a vague name and you need to find the exact page title
- You want to cross-reference a new experiment against a previous one already in LabArchives
- You need context from adjacent notebook pages

**Resolution order for ambiguous requests:**
1. Search Slack history for relevant links or context
2. Search LabArchives by approximate page title
3. Only ask the user if both fail

## Step 1 — verify .env is populated

Read the required key names from `lab_config.md`, then run:

```bash
python -c "
from dotenv import dotenv_values
env = dotenv_values('$PROJECT_ROOT/.env')
for k in ['GITHUB_TOKEN','LA_AKID','LA_SECRET','LA_UID']:
    print(k + ': ' + ('set' if env.get(k) else 'MISSING'))
"
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

After `run.py` finishes and DR conditions are decided (Step 2b), generate the report by spawning **Agent-tool subagents**. Do **not** write the report yourself.

Run the four phases in strict sequence: **Phase A** (four analysts, concurrent) → **Phase B** (waits for all of A) → **Phase C** (waits for B) → **Phase D** (waits for C; may trigger one Phase C revision).

**Conventions for every subagent below:**
- Let `<out_dir>` = `$PROJECT_ROOT/outputs/<experiment_id>`. Substitute it literally into each prompt.
- Each agent reads its inputs from `<out_dir>` and writes its output file(s) back into `<out_dir>`.
- To analyse figures, the agent **reads the image files directly with the Read tool** (it renders images natively) — never base64-encode anything.
- Spawn agents with the Agent tool (general-purpose subagent). **Spawn the Phase A agents together in a single message so they run concurrently.** Wait for a phase to fully complete before starting the next.

> **This branch is agents-only.** A direct-API implementation of these same four phases (using `ANTHROPIC_API_KEY` instead of Claude Code subagents) exists separately on the `conference-api-version` branch as `run_phase_a/b/c/d.py`, for environments without a Claude Code plan. It is intentionally not part of this branch — always orchestrate the phases via the Agent tool as described below.

---

### Phase A — Parallel extraction (4 concurrent subagents)

Spawn all applicable analysts in **one message**. Spawn the **DR Analyst only if `<out_dir>/dr_conditions.md` exists**.

**GitHub Analyst** → writes `<out_dir>/extracted_github.md`

> You are the GitHub Analyst. Read `<out_dir>/notebooks.md`, `<out_dir>/data_summaries.md`, and every image file listed in `<out_dir>/github_images.md` (open each image with the Read tool). Produce a single Markdown document with exactly these sections (use ## headings) and write it to `<out_dir>/extracted_github.md`:
>
> **## Key Parameters** — a markdown table: Parameter | Value | Units | Notes. Every instrument setting, frequency range, amplitude, power level, timing parameter, and software constant found anywhere. If a value appears multiple times with different numbers, list both and note the discrepancy.
> **## Sweep Procedure** — ordered list of what the code actually does step by step. Interpret imported classes/functions from their names and usage — do not guess at internals not visible in the notebooks.
> **## Numeric Results** — bulleted list of every quantitative outcome with units: fitted resonance frequencies, Q factors, power levels, measured ranges, calibration values.
> **## Figures** — one sub-section (### filename) per image. Describe what is visually present: axis labels, curve shapes, legend entries, numeric values readable in the plot. One-line physical interpretation. If an image cannot be read write [Image not readable].
> **## Cross-reference flags** — values needing validation against other sources: attenuation assumptions baked into amplitude settings, frequency references that should match LabArchives notes, timestamps that could correlate with DR data.

**LabArchives Analyst** → writes `<out_dir>/extracted_labarchives.md`

> You are the LabArchives Analyst. Read `<out_dir>/labarchives.md` and every image file listed in `<out_dir>/labarchives_images.md` (open each with the Read tool). Also fetch the wiring diagram page live: its title is the `Wiring diagram page` value in `$PROJECT_ROOT/lab_config.md`; fetch it by running (from `$PROJECT_ROOT`, so `.env` credentials load regardless of how the session was launched) `python -c "from dotenv import load_dotenv; load_dotenv('.env'); from lab_agent.sources.labarchives import LabArchivesAdapter; arts=LabArchivesAdapter('<WIRING_DIAGRAM_PAGE>').fetch(); print('\n\n'.join(a.content for a in arts if a.content))"`. Produce a single Markdown document with exactly these sections (## headings) and write it to `<out_dir>/extracted_labarchives.md`:
>
> **## Timeline** — chronological list of actions/observations with timestamps as they appear in the notebook.
> **## Lab Observations** — what was actually observed or noted, past tense, sourced from what the notebook says happened.
> **## Stated Goals** — clearly labelled NOT necessarily executed. Every future-tense statement, "plan to", "optionally", "next we will". These are intentions, not actions.
> **## All Hyperlinks** — every [text](url) link found anywhere; preserve full URLs; note what each points to.
> **## Additional GitHub URLs** — any GitHub links beyond the primary one; note whether each appears relevant based on context.
> **## Attenuation Chain** — from the wiring diagram: full RF component list in signal-path order with the dB value for each stage and the total attenuation.
> **## Discrepancies** — any attenuation/power value in the lab notes that conflicts with the wiring diagram; quote both values exactly and note which source is the diagram.
> **## Figures** — one sub-section (### filename) per image. Describe visual content + physical interpretation. Write [Image not readable] if the file cannot be opened.
> **## Cross-reference flags** — values in the notes needing validation against GitHub notebooks: power levels assumed in notes, frequency references, timing windows.

**Dependencies Analyst** → writes `<out_dir>/extracted_deps.md`

> You are the Dependencies Analyst. Read `<out_dir>/dependencies.md`. Produce a single Markdown document and write it to `<out_dir>/extracted_deps.md`. One ## section per package: what the package does, key classes with their constructor parameters and defaults, key hardware constants defined in the source, data flow through the package's main entry points. Note any packages marked "Not found in org" and describe what their import usage in the notebooks suggests about their role. End with **## Cross-reference flags** — any constant whose default value in the source differs from what appears to be set explicitly in the notebooks.

**DR Analyst** *(only if `<out_dir>/dr_conditions.md` exists)* → writes `<out_dir>/extracted_dr.md`

> You are the DR Analyst. First read the **"DR Analyst — physics reference"** section of `$PROJECT_ROOT/CLAUDE.md` for the physics you need. Then read `<out_dir>/dr_conditions.md`. Produce a single Markdown document with exactly these sections (## headings) and write it to `<out_dir>/extracted_dr.md`:
>
> **## System State** — at base, cooling, or warming during the window? One clear sentence.
> **## Temperature Analysis** — MXC min/median/max with units, ratio of max to min, which channels report valid readings vs. known-unreliable at base (RuO2 sensors — Still, 50 mK plate — lose calibration below ~1 K; their absence is normal).
> **## Pressure Analysis** — P1 fore-line value, whether pumps were running, any fluctuations and what they indicate.
> **## n_th calculation** — thermal photon occupancy at 5 GHz at the MXC median temperature using n_th = 1/(exp(hf/kT) − 1). Show the arithmetic.
> **## Quasiparticle assessment** — Mattis-Bardeen: estimate thermal QP contribution relative to base and what it means for the experiment type. For Al: Δ ≈ 172 μeV ≈ 2 K equivalent; n_qp ∝ exp(−Δ/kT).
> **## Anomaly flags** — each applicable anomaly with its physical explanation (MXC min > 50 mK; max/min > 3×; median >> min; Still > 1 K; P1 at ~1000 mbar).
> **## Cross-reference flags** — time windows where the DR was anomalous that should be checked against experiment timestamps in the GitHub notebooks.

---

### Phase B — Synthesis (1 subagent)

After all Phase A agents finish, spawn one Synthesis agent → writes `<out_dir>/connections.md`.

> You are the Synthesis Agent. Read only the extracted files in `<out_dir>` (`extracted_github.md`, `extracted_labarchives.md`, `extracted_deps.md`, and `extracted_dr.md` if present) — not the raw data. Find every cross-source connection, conflict, and gap. Write `<out_dir>/connections.md` with exactly these sections (## headings):
>
> **## Power chain closure** — combine the attenuation chain (extracted_labarchives.md) with amplitude/power settings (extracted_github.md); calculate power at the device (dBm); does the math close? State result with units and any discrepancy.
> **## Timeline correlations** — map experiment steps (extracted_github.md) against DR anomaly windows (extracted_dr.md): was any measurement taken during an elevated-temperature or pump-off period? If no DR data, state that explicitly.
> **## Lab observations vs. numeric results** — does extracted_labarchives.md corroborate or contradict the fitted/measured values in extracted_github.md? Agreements and conflicts separately.
> **## Goal vs. executed map** — a table, one row per stated goal from extracted_labarchives.md: Stated Goal | Evidence of Execution in Notebooks (yes/no/partial) | Source line.
> **## Dependency constants vs. notebook usage** — using extracted_deps.md cross-reference flags, list every case where a notebook sets a value differing from the package default.
> **## Multi-source conflicts** — any numeric value appearing in more than one extracted file with different numbers; exact values from each source; which to trust and why.
> **## Figures needing cross-source context** — any figure whose interpretation depends on information from a different extracted file; explain the dependency.
> **## Additional GitHub URLs recommendation** — based on all extracted files, recommend whether any additional GitHub URLs flagged by the LabArchives Analyst are worth fetching before writing; if yes, which and why.

---

### Phase C — Report writing (1 subagent)

After Phase B finishes, spawn one Report Writer agent → writes `<out_dir>/[UNSIGNED] <experiment_id>.md`.

> You are the Report Writer. Read the extracted files and `connections.md` in `<out_dir>`, plus `<out_dir>/metadata.json` (for `experiment_id` and `la_pages`). Do **not** read raw data files and never read existing `[UNSIGNED]` files. Follow the **"Report Writer — instructions"** section of `$PROJECT_ROOT/CLAUDE.md` exactly — header block, section order, figure-embedding rules, and accuracy/claims rules. Embed figures with relative paths (e.g. `![caption](labarchives_images/filename.png)`) reading figure descriptions only from the extracted files. Write the finished report to `<out_dir>/[UNSIGNED] <experiment_id>.md`. Output must start directly with the `# [UNSIGNED] <experiment_id>` title line — no preamble, no filename, no `.md` extension in the title.

---

### Phase D — Critique and revision

After Phase C finishes, spawn one Critic agent → writes `<out_dir>/critique.md`.

> You are the Critic. Read the `[UNSIGNED]` report, all `extracted_*.md`, and `connections.md` in `<out_dir>`. Run this fixed checklist against the report; for each item write PASS or FAIL, and for every FAIL quote the exact failing sentence and explain why. Write the result to `<out_dir>/critique.md`.
>
> 1. Every numeric value in the report's Key Parameters table appears with the same number in extracted_github.md or extracted_deps.md.
> 2. No figure description in the report contains visual content not present in the corresponding Figures sub-section of an extracted file.
> 3. "Confirms" is not used unless connections.md documents a direct quantitative comparison that supports it.
> 4. No step is described as executed that appears in the Goal vs. Executed map in connections.md as "no".
> 5. No physics mechanism is named that does not appear in any extracted file.
> 6. The Key Parameters table appears exactly once in the report.
> 7. The Key Findings section does not restate sentences that already appear in Results.
>
> End with a `## Summary` line: `PASS` (all items passed) or `FAIL` (list the failing item numbers).

Read `critique.md`:
- **All items PASS** → proceed directly to Step 4 (upload).
- **Any item FAILs** → run one revision pass: spawn a Report Writer agent in revision mode →

> You are the Report Writer performing a targeted revision. Read the current `[UNSIGNED]` report in `<out_dir>`, `<out_dir>/critique.md`, and the `extracted_*.md` + `connections.md` files. Fix ONLY the FAIL items from the critique — make the minimum changes necessary; do not restructure sections that PASSED or rewrite passing sentences. Overwrite the same `[UNSIGNED] <experiment_id>.md`. Output ONLY the complete revised report, starting directly with the `# [UNSIGNED]` header — no preamble, no "Fixes Applied" section, no commentary.

Then proceed to Step 4.

---

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

## DR Analyst — physics reference

*This section is read by the DR Analyst agent (Phase A) and by the DR-Only Report writer. It contains the physics knowledge needed to interpret dilution refrigerator data.*

### What the channels mean

- **MXC (CMN 172)** — the mixing chamber temperature, measured by a paramagnetic salt (CMN) thermometer. This is the most physically meaningful number: it is the temperature the chip actually sees. Valid below ~400 mK; at base typically 10–50 mK. This is the only channel that reads correctly at true base temperature — RuO2 sensors (Still, 50 mK plate) lose calibration below ~1 K and return garbage values, so their absence from the table at base is normal and expected.
- **Still (~700 mK)** — the still pot, where 3He evaporates to drive circulation. A healthy still runs at 0.6–0.9 K. Values are only valid during cooling/warming transitions; at base the RuO2 calibration fails.
- **4K stage (TT-2450)** — the 4 K cold plate. Valid 2–300 K range. At base it reads garbage (same calibration issue).
- **Pressures (P1–P4)** — P1 is the fore-line. When the pumps are running, P1 should be a few mbar. P1 ≈ 1000 mbar (atmospheric) means the fore-pump was off — no circulation, system warming. Negative values are gauge offsets and can be ignored.

### What base temperature means for the experiment

**Thermal photon population in the qubit/resonator:**
The condition for a clean quantum experiment is kT << hf. For a 5 GHz qubit:
- At 20 mK: kT/h ≈ 415 MHz → hf/kT ≈ 12 → thermal photon occupancy n_th ≈ e^{-12} ≈ 0.0001% — negligible
- At 100 mK: kT/h ≈ 2.1 GHz → hf/kT ≈ 2.4 → n_th ≈ 9% — the qubit has a ~9% chance of being thermally excited even without any drive. This is a serious source of readout error and apparent T1 degradation.
- At 200 mK: n_th ≈ 30% — the qubit is essentially a classical thermal mixture. T1 and T2 measurements at this temperature are not representative of intrinsic coherence.

**Quasiparticle poisoning (for both qubits and KIDs):**
Superconductors have a gap Δ. For aluminum (Al): Δ ≈ 172 μeV ≈ 2 K equivalent. At base temperature, the thermal quasiparticle density is exponentially suppressed: n_qp ∝ exp(-Δ/kT). At 20 mK this is essentially zero and non-equilibrium quasiparticles (from radiation, cosmic rays, phonon bursts) dominate. But if MXC was elevated:
- At 100 mK: thermal n_qp begins to contribute meaningfully → increased quasiparticle-induced T1 loss (1/T1 ∝ n_qp × |g|²) and KID noise floor rises
- At 200 mK: thermal quasiparticles exceed non-equilibrium ones → T1 times collapse, KID responsivity degrades
- This is why even a 50 mK elevation from 20 mK to 70 mK matters: n_qp changes by exp(-Δ/k × (1/70mK - 1/20mK)) ≈ exp(-220), but relative to non-equilibrium background, still significant

**For KID experiments specifically (Mattis-Bardeen physics):**
KID resonant frequency and quality factor both depend on temperature through the complex conductivity σ₁ + iσ₂. Even small temperature increases shift f₀ and degrade Qi via increased quasiparticle density. The Mattis-Bardeen fitting functions in `daq/analysis/mattis_bardeen.py` assume thermal equilibrium — if the DR was not at a stable base temperature during the sweep, fitted Δ₀ and α values will be systematically wrong.

### Anomalies to flag and what they mean

**MXC min > 50 mK when qubit T1/T2 or KID spectroscopy was the goal:**
The system did not reach proper base. Thermal photon population and quasiparticle density are elevated. All coherence times and quality factors measured are lower bounds, not intrinsic values.

**Large spread between MXC min and max (ratio > 3×) within the window:**
The system was either still cooling, had a thermal event (vibration burst, cosmic ray event, pulse tube hiccup), or the measurement was taken during warmup. If data was taken during this instability, resonator fits may have frequency drift baked in. Measurements taken at the coldest point are most reliable.

**MXC median >> MXC min:**
The system spent most of the window warmer than its coldest point — likely cooling down or warming up during the measurement. Reproducibility is questionable.

**Still temperature > 1 K (when readable):**
The still is running hot — reduced 3He circulation (pump issue, partial blockage) or still heater deliberately raised. Cooling power at the MXC is reduced, which explains elevated base temperature.

**P1 (fore-line) at atmospheric (~1000 mbar) during part of the window:**
The fore-pump was off. The dilution circuit was not circulating — the MXC was cooling only from thermal mass, not active cooling. Any measurements during this period were taken with a warming system.

**No valid temperature readings at all:**
The DR was either at room temperature, mid-cooldown, or the Leiden Cryogenics software was not logging. Do not infer cold conditions — state explicitly that DR temperature during this experiment is unknown.

### How to incorporate DR conditions into the report

- **Methods section:** State the base temperature and time window factually. e.g. "The dilution refrigerator reached a base temperature of 14.2 mK (median 30.1 mK over the measurement window) as measured by the CMN 172 thermometer at the mixing chamber."
- **Key Parameters table:** Add a row for MXC temperature (min and median).
- **Results section:** If temperature was stable and cold, note that thermal effects are negligible. If anomalous, quantify the impact using the physics above.
- **Open Questions:** If temperature instability was observed, suggest a follow-up run at confirmed stable base temperature.

---

## Report Writer — instructions

*This section is read by the Report Writer agent (Phase C) and by the DR-Only Report writer. Follow it exactly.*

### Embedding images in the report
- Embed figures using relative Markdown paths: `![caption](labarchives_images/filename.png)`
- Place each image directly after the paragraph that discusses it.
- Use a descriptive caption that identifies what the figure shows.
- **Only embed figures you directly cite in Results or Key Findings.** Do not embed every figure from the extracted files — only the ones whose content you explicitly interpret in the text. Methods and Open Questions sections should rarely have embedded figures.
- **Order matters for upload:** LabArchives inlines images in document order up to a ~640 KB budget (~5–12 real figures). Place the most critical figures first — primary result plots before supporting or diagnostic ones. A figure cited in Results ranks above one cited in Methods.
- Skip duplicates, thumbnail-quality images, and any figure whose description adds nothing beyond what the surrounding text already states.

### Report title and metadata
Always begin the report with:
```
# [UNSIGNED] <experiment_id>

**Experiment:** <experiment_id>
**LabArchives pages:** <page_1> · <page_2> · ...
**Report generated:** <YYYY-MM-DD>
```
For example:
```
# [UNSIGNED] power_calibration_20260227

**Experiment:** power_calibration_20260227
**LabArchives pages:** 20260303 Amplitude & DAC Sweep · 20260304 Power Calibration Sweep
**Report generated:** 2026-04-09
```

### Report sections

Place a horizontal rule (`---`) between every section for visual separation in LabArchives.
All sections in full paragraphs unless noted otherwise.

**1. Table of Contents**
A short linked list of all section headings so readers can jump directly to any section.

**2. Executive Summary**
One paragraph: what was done, key result, and one-line physical interpretation.

**3. Objectives**
State the specific experiment objectives as a short bulleted list (e.g. "identify resonance frequencies by VNA", "measure parity telegraph timestream"). Written for lab members already expert in the platform — no general-audience explanation of basic concepts. Do not explain what a qubit is, what dispersive readout is, or any other concept the lab already knows. Focus only on what makes this specific experiment distinct from prior runs.

**4. Key Parameters**
A single formatted table of all critical instrument settings and software constants extracted from `extracted_github.md` and `extracted_deps.md`, with RF attenuation values from `extracted_labarchives.md`. Use diagram values where they conflict with notebook values (discrepancies are documented in `connections.md`). Include DR temperature row if `extracted_dr.md` is present.
This table appears once and only once. Do not create a second numeric summary table in Results.

**5. Methods and Workflow**
Instruments, software, and processing steps in prose. Use bold sub-headers for each major phase
(e.g. `**Instruments.**`, `**Reference clock synchronization.**`, `**Sweep procedure.**`, `**Data processing.**`).
Incorporate dependency knowledge from `extracted_deps.md` naturally — explain what imported classes/functions actually do, including their default parameters and hardware configuration.

**6. Results**
Interpret the numbers; reference specific values; note trends and anomalies; embed relevant figures.
For each embedded figure: (a) use only the description from the corresponding "Figures" sub-section of an extracted file — if none exists, write `[Image not available — not described in extraction]`; (b) give one sentence of physical interpretation.

**7. Key Findings and Interpretation**
What the results mean physically — go deep. Use `connections.md` to ground cross-source interpretations. Distinguish confirmed findings from inferences. Do not restate the Results narrative — this section interprets causality and underlying physics, not re-describes what happened.

**8. Dilution Refrigerator Conditions** *(include only if `extracted_dr.md` is present)*
Summarise the system state, temperature stability, and physical implications for the measurement. Use the content from `extracted_dr.md` directly — do not reinterpret raw data.

**9. Open Questions**
Bullet list of unresolved issues, anomalies worth investigating, and follow-up experiments suggested by the results. Include anything flagged in `connections.md` that could not be resolved.

**10. Sources**
Bullet list of all artifacts used, with clickable links where possible:
- GitHub repo and individual notebooks: link to the GitHub URL provided
- LabArchives pages: page title (link to `https://mynotebook.labarchives.com/` if URL known)
- Local output files: filename only
- Images: grouped by source (GitHub images / LabArchives images), listed by filename

### Rules

**Accuracy and sourcing**
- Never invent results or describe measurements that did not occur
- Do not infer or name physics mechanisms (e.g. Andreev reflection, Andreev levels, Josephson inductance shifts) unless an extracted file explicitly mentions them
- Distinguish executed operations from stated goals: the Goal vs. Executed map in `connections.md` is authoritative — if a goal appears as "no" there, do not report it as executed; note the gap explicitly
- Distinguish observed facts from inferences ("the data show..." vs "this suggests...")
- Never read `[UNSIGNED]` files from the outputs folder as source material — they are prior AI-generated drafts and may contain errors

**Claims and strength of language**
- When using the word "consistent", always specify whether the agreement is qualitative (same trend, same order of magnitude) or quantitative (within X% of predicted value)
- Do not use "confirms" or "confirms that" unless `connections.md` documents a direct quantitative comparison; use "is consistent with" or "suggests" otherwise
- Optimistic bias is a known failure mode: do not default to "the device is working as expected" without explicitly stating what evidence supports that claim and what evidence is missing

**Figures**
- Only describe figure content present in the extracted file's "Figures" sub-section
- If an image is not described in an extracted file, write `[Image not available — not described in extraction]` — do not invent colors, curves, labels, or features
- Do not duplicate figure captions across sections

**Structure and conciseness**
- Key Parameters table appears once only — do not create a second numeric summary in Results
- Key Findings must not restate Results — if a sentence could appear in both, it belongs only in Results
- Keep prose tight: one well-chosen sentence beats two vague ones
- Scale depth to the data — a quick single-sweep does not need a long report

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

Use a 3-agent pipeline — spawn agents sequentially (each waits for the previous):

**1. DR Analyst agent**
Reads `dr_conditions.md` and the **DR Analyst — physics reference** section of this file.
Writes `outputs/dr_YYYYMMDD/extracted_dr.md` following the same schema as Phase A above.

**2. Report Writer agent**
Reads `extracted_dr.md` only. Writes `outputs/dr_YYYYMMDD/[UNSIGNED] dr_YYYYMMDD.md` following the **Report Writer — instructions** section of this file, using the DR-only section structure below.

**3. Critic agent**
Checks: no invented temperature values, no anomaly flags not present in `extracted_dr.md`, no physics mechanisms not mentioned there. If any fail, one revision pass.

#### DR-only report sections

**1. Header**
```
# [UNSIGNED] dr_YYYYMMDD

**Date/window:** <date range>
**Report generated:** <YYYY-MM-DD>
```

**2. Summary**
One paragraph: what the DR was doing during this window, key temperatures, and whether the system was at base, cooling, or warming.

**3. Temperature Conditions**
Full prose interpretation using `extracted_dr.md`. Include the data table from `dr_conditions.md`. For the MXC: state base temperature, assess stability (min vs median vs max spread), state the n_th calculation from `extracted_dr.md`, and the quasiparticle assessment.

**4. Pressure Conditions**
Interpret the pressure data from `extracted_dr.md`. Note whether the fore-pump was running, any fluctuations, and what they indicate about system health.

**5. System State Assessment**
A clear one-paragraph verdict: was the system in a good state for measurements? Would qubit T1/T2 or KID spectroscopy data taken at this time be trustworthy? Flag any anomalies with physical explanations drawn from `extracted_dr.md`.

**6. Open Questions / Follow-up**
Any anomalies worth investigating, or observations that suggest follow-up action.

### Step C — upload to LabArchives

```bash
cd $PROJECT_ROOT
python upload_to_labarchives.py outputs/dr_YYYYMMDD
```
