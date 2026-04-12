# Lab Agent — Pipeline Instructions

When the user gives you a GitHub URL and LabArchives page names, run the pipeline and write the report.

## Step 1 — verify .env is populated

```bash
python -c "
from dotenv import dotenv_values
env = dotenv_values('C:/Users/axelr/OneDrive/Desktop/lab-agent-single-report/.env')
for k in ['GITHUB_TOKEN','LA_AKID','LA_SECRET','LA_UID']:
    print(k + ': ' + ('set' if env.get(k) else 'MISSING'))
"
```

Only report set or missing — never print the values themselves.
If any are missing, tell the user which ones to fill in before proceeding.

## Step 2 — run the pipeline

```bash
cd C:/Users/axelr/OneDrive/Desktop/lab-agent-single-report
python run.py "<github_url>" "<la_page_name_1>" "<la_page_name_2>"
```

- LabArchives inputs can be page titles (case-insensitive), raw base64 tree_ids, or notebook URLs.
- Output lands in `outputs/<experiment_id>/` with:
  - `notebooks.md` — all notebook content
  - `labarchives.md` — lab notes from LabArchives
  - `data_summaries.md` — CSV contents and numeric ranges
  - `dependencies.md` — source code of lab-specific imports found in the org
  - `github_images/` + `github_images.md` — images downloaded from the GitHub repo
  - `labarchives_images/` + `labarchives_images.md` — images from LabArchives (attachments + embedded)

## Step 3 — read the files and write the report

Read all files in the output folder, including images, then write a full prose report and save it to
`outputs/<experiment_id>/[UNSIGNED] <experiment_id>.md`.
For example: `outputs/power_calibration_20260227/[UNSIGNED] power_calibration_20260227.md`

### Reading dependencies.md
- Read `dependencies.md` — it contains source code for lab-specific packages (e.g. `daq`, custom measurement classes).
- Use it to understand what the imported functions/classes actually do: default parameters, hardware configuration, data flow.
- Incorporate this naturally into the Methods section — e.g. "the `Sweep` class configures the Presto with `DAC_CURRENT = 40_500 µA` by default".
- If a package was not found in the org (external), note it briefly.

### Reading images
- Read `github_images.md` and `labarchives_images.md` to get the list of image paths.
- Use the Read tool on each image file to view it (Claude is multimodal and can interpret plots, screenshots, etc.).
- Describe what each image shows and work its content into the relevant report section.

### Embedding images in the report
- Embed figures using relative Markdown paths: `![caption](labarchives_images/filename.png)`
- Place each image directly after the paragraph that discusses it.
- Use a descriptive caption that identifies what the figure shows.
- Only embed images that add meaningful context; skip duplicates or uninformative ones.

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

**3. Background and Objective**
Why this experiment matters physically — connect to the broader qubit/KID research context.
State the specific objectives (e.g. "determine optimal DAC_CURRENT", "map power vs frequency").

**4. Key Parameters**
A formatted table of all critical instrument settings and software constants extracted from the notebooks and dependencies:
- Instrument settings (DAC_CURRENT, RBW, settle time, frequency range, amplitude range, etc.)
- Software constants (from `daq`, `presto`, or any lab package)
- Derived values used downstream
This gives any lab member everything needed to reproduce the experiment at a glance.

**5. Methods and Workflow**
Instruments, software, and processing steps in prose. Use bold sub-headers for each major phase
(e.g. `**Instruments.**`, `**Reference clock synchronization.**`, `**Sweep procedure.**`, `**Data processing.**`).
Incorporate dependency source code naturally — explain what imported classes/functions actually do,
including their default parameters and hardware configuration.

**6. Results**
Interpret the numbers; reference specific values; note trends and anomalies; embed relevant figures.
Always include a markdown summary table of the key numeric results (e.g. per-band power ranges, fit parameters).
For each embedded figure, add a caption that (a) describes what the plot shows and (b) gives one sentence of physical interpretation.

**7. Key Findings and Interpretation**
What the results mean physically — go deep. Connect observations to underlying physics
(e.g. impedance matching, harmonic distortion mechanisms, thermal noise, resonator behavior).
Distinguish confirmed findings from inferences.

**8. Credibility Notes**
Limitations and data quality issues in prose. Cover: missing data, unverified assumptions,
instrument accuracy, anything that could affect reproducibility.

**9. Reproducibility Checklist**
A bullet list of everything needed to exactly reproduce this experiment:
hardware configuration, software versions, calibration state, any setup steps that must be done first.

**10. Open Questions**
Bullet list of unresolved issues, anomalies worth investigating, and follow-up experiments suggested by the results.

**11. Sources**
Bullet list of all artifacts used, with clickable links where possible:
- GitHub repo and individual notebooks: link to the GitHub URL provided
- LabArchives pages: page title (link to `https://mynotebook.labarchives.com/` if URL known)
- Local output files: filename only
- Images: grouped by source (GitHub images / LabArchives images), listed by filename

### Rules
- Never invent results
- Distinguish observed facts from inferences explicitly ("the data show..." vs "this suggests...")
- Go deep on physics — use your knowledge of superconducting qubits, KIDs, and RF measurement
- Integrate LabArchives notes naturally — do not quote them as raw blocks
- If something is missing or uncertain, say so explicitly
- Scale depth to the data — a rich multi-session experiment deserves a longer report than a quick single-sweep

## Step 4 — upload the report to LabArchives

After saving the report, run:

```bash
cd C:/Users/axelr/OneDrive/Desktop/lab-agent-single-report
python upload_to_labarchives.py outputs/<experiment_id>
```

This will:
1. Find the **AI Agent** folder in the Qubit & KID notebook.
2. Create a new page named after the experiment (the output folder name).
3. Post the report as a rendered HTML rich-text entry.
4. Attach the raw `.md` file.

If the upload fails, report the error to the user — do not silently skip it.

## Notebooks available (Qubit & KID)
The user's primary notebook is **Qubit & KID**. Pages searched by title automatically.
Other notebooks: QUALIPHIDE, NK - External & Internal Shares.

## Known limitation
Web app page IDs (e.g. `11400322`) do **not** map to API tree_ids.
Pass page titles or base64 tree_ids instead.
