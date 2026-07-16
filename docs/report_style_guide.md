# Report Writer — instructions

*This document is read by the Report Writer agent (Phase C) and by the DR-Only Report writer. Follow it exactly.*

## Embedding images in the report
- Embed figures using relative Markdown paths: `![caption](labarchives_images/filename.png)`
- Place each image directly after the paragraph that discusses it.
- Use a descriptive caption that identifies what the figure shows.
- **Only embed figures you directly cite in Results or Key Findings.** Do not embed every figure from the extracted files — only the ones whose content you explicitly interpret in the text. Methods and Open Questions sections should rarely have embedded figures.
- **Order matters for upload:** LabArchives inlines images in document order up to a ~640 KB budget (~5–12 real figures). Place the most critical figures first — primary result plots before supporting or diagnostic ones. A figure cited in Results ranks above one cited in Methods.
- Skip duplicates, thumbnail-quality images, and any figure whose description adds nothing beyond what the surrounding text already states.

## Report title and metadata
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

## Report sections

Place a horizontal rule (`---`) between every section for visual separation in LabArchives.
All sections in full paragraphs unless noted otherwise.

**1. Table of Contents**
A short linked list of all section headings so readers can jump directly to any section.

**2. Executive Summary**
One paragraph: what was done, key result, and one-line physical interpretation.

**3. Objectives**
State the specific experiment objectives as a short bulleted list (e.g. "identify resonance frequencies by VNA", "measure parity telegraph timestream"). Written for lab members already expert in the platform — no general-audience explanation of basic concepts. Do not explain what a qubit is, what dispersive readout is, or any other concept the lab already knows. Focus only on what makes this specific experiment distinct from prior runs.

**4. Key Parameters**
A single formatted table of all critical instrument settings and software constants extracted from `extracted_github.md` and `extracted_deps.md`, with RF attenuation values from `extracted_labarchives.md`. Use the columns `Parameter | Value | Units | Notes | Source`, where **Source** names where each value came from (e.g. `GitHub notebooks`, `dependency source`, `lab notes`, `wiring diagram`, `DR data`) and corresponds to the extracted file it was taken from. Use diagram values where they conflict with notebook values (discrepancies are documented in `connections.md`). Include DR temperature row if `extracted_dr.md` is present.
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

## Rules

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

## DR-only report sections

*Used by the DR-Only Report workflow (no experiment, no GitHub URL). The rules above (accuracy, claims, conciseness) still apply; the section structure is:*

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
