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

## Which template — brief is the default

**Write the brief (plain-language) report unless the request says "full" / "detailed".** This is the lab's stated preference: of everything the pipeline has produced, the plain-language version was picked as the best, and "shorter / drop the Key Parameters table / plain units" has been asked for on separate occasions. Defaulting to it removes the most common reason a report needs a second pass.

- **Brief (default)** — "Brief report sections" below. Readable by the whole group, every exact number retained.
- **Full** — "Full report sections" below. Use only when explicitly requested, or when the report is a formal record for an external audience.

Both templates obey the same **Rules** section: sourcing, claim strength, and figure honesty do not relax with the format. Both also require the sourcing sidecar (`provenance.md`) and the Slack summary (`slack_summary.md`).

---

## Brief report sections (default)

Place a horizontal rule (`---`) between sections.

**Header block** — same as the full template (title, `**Experiment:**`, `**LabArchives pages:**`, `**Report generated:**`).

**1. What We Did**
Two or three short paragraphs of plain language: what was compared or measured, why, what changed since any previous run, and the conditions (frequencies, power range, instruments). Name the instruments and say what they are on first use — a labmate outside this experiment should follow it.

**2. Setup / Calibration** *(only when there is a setup worth stating)*
What had to be true before the measurement means anything. Embed the calibration artifact here if there is one (a notebook photo, a settings table). End with a bold **Key takeaway:** line.

**3. Main Result: \<state the result in the heading\>**
The heading itself carries the finding — "Main Result: Presto and VNA Agree Well", not "Results". Give the numbers with units, embed the figure that shows it directly beneath the claim it supports, and end with a bold **Bottom line:** line saying what it means practically.

**4. New Finding: \<state the finding in the heading\>** *(repeat per finding)*
One section per genuinely new observation, heading-as-conclusion again ("New Finding: VNA Double Peak = VNA Output Problem (Not the Reference Clock)"). Say what was tried, what was seen, and what it rules in or out. Keep the honest limits — "we don't yet know why" belongs here, not in a footnote.

**5. What This Tells Us** *(optional)*
Cross-run or cross-condition implications that do not belong to a single finding — e.g. what comparing with and without a component reveals about that component.

**6. Open Questions**
Short bullets, plain terms, one line each.

**7. Sources**
Bullet list: notebooks (link to the GitHub URL, pinned to the commit SHA from `metadata.json`), LabArchives page titles, key photos or data files. No citation markers — the sidecar carries provenance.

### Brief-template rules

- **Plain language, exact numbers.** Simplify the prose, never the data: keep every value, unit, and range exactly as the extracted files give them. "Within ±0.35 dBm" is fine; "about a third of a dB" is not.
- **No Table of Contents, no Key Parameters table, no Methods section, no Citations section.** Parameters that matter appear in the sentence that uses them.
- **Every figure sits at the claim it supports**, with a caption saying what to *look at*, not merely what the image is.
- **Bold "Bottom line:" / "Key takeaway:"** after each major block — that line is what a busy reader takes away.
- Same figure-ordering rule as the full template: most important first (the LabArchives inline budget is spent in document order).
- Typically 80–120 lines. If it is much longer, it is drifting back toward the full template.

---

## Full report sections (on request)

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
- GitHub repo and individual notebooks: link to the GitHub URL provided. When `metadata.json` has `github_commit_sha`, pin the provenance by citing the 7-char short SHA and the `github_commit_date` as the snapshot date (e.g. "daq@`9f11532`, 2026-02-27").
- LabArchives pages: page title (link to `https://mynotebook.labarchives.com/` if URL known)
- Local output files: filename only
- Images: grouped by source (GitHub images / LabArchives images), listed by filename
- If `metadata.json` shows `github_tree_truncated: true`, note that some repo files may be missing from this report.

**11. Citations**
A numbered list mapping each `[n]` marker used in the report to its exact source — a LabArchives page + entry, a notebook cell (`## Cell N` of a file in `notebooks.md`), a data file, a dependency constant, or the DR data. **Every Key Parameters row carries a `[n]`** resolving here; other key numeric claims should too where practical. Never cite a link marked `not_found`/`unreachable` in `link_check.md` — write `[MISSING: <url>]` instead. (This formalizes the `Source` column into checkable claim→source links.)

## The sourcing sidecar — `provenance.md` (both templates)

The full template proves provenance in the reader's face, with a `Source` column and `[n]` markers. The brief template drops both — so provenance moves to a sidecar instead of evaporating. Write `<out_dir>/provenance.md` alongside every report:

```markdown
# Sources for [UNSIGNED] <experiment_id>

| Value or claim | Where in report | Extracted file | Source line |
|---|---|---|---|
| +0.165 dB mean offset at 6.9 GHz | Main Result | extracted_github.md | "Presto 6.9 GHz calibration mean offset: +0.165 dB" |
| −75 to −83 dBm Presto spur floor | New Finding: spurious tones | extracted_labarchives.md | "spurs at −75 to −83 dBm when active" |
```

- **One row per numeric value and per substantive claim** in the report body.
- **Source line** quotes the extracted file, so the critic can match it without re-deriving anything.
- A value that cannot be given a row does not belong in the report.
- This file is for the critic and for auditing — it is never uploaded to LabArchives and never shown to the user.

Net effect: the reader gets a clean document and the auditor keeps a complete claim→source map. This is stricter than the old `[n]` scheme, which only required citations on Key Parameters rows.

---

## Rules

**Accuracy and sourcing**
- Never invent results or describe measurements that did not occur
- Do not infer or name physics mechanisms (e.g. Andreev reflection, Andreev levels, Josephson inductance shifts) unless an extracted file explicitly mentions them
- Distinguish executed operations from stated goals: the Goal vs. Executed map in `connections.md` is authoritative — if a goal appears as "no" there, do not report it as executed; note the gap explicitly
- Distinguish observed facts from inferences ("the data show..." vs "this suggests...")
- Never read `[UNSIGNED]` files from the outputs folder as source material — they are prior AI-generated drafts and may contain errors
- Never cite a link marked `not_found` or `unreachable` in `link_check.md` — write `[MISSING: <url>]` instead of the link

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
