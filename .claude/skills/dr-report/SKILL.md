---
name: dr-report
description: Produce a standalone dilution-refrigerator conditions report for a date or time window, with no experiment (no GitHub URL). Use when the user asks e.g. "give me a DR report for Feb 18 2025", "what were the DR conditions on Feb 18 between 2pm and 10pm?", or "log the cooldown from Feb 17–19 2025". Runs run_dr.py, a 3-agent pipeline (dr-analyst → report-writer → critic), and uploads to LabArchives. For "how's the DR right now?" (no report) use dr-status instead.
---

# DR-only report workflow

Use when the user asks for a dilution refrigerator conditions report without any experiment. Read `lab_config.md` (per CLAUDE.md) for `$PROJECT_ROOT`.

## Step A — run the DR parser

```bash
cd $PROJECT_ROOT
python run_dr.py "YYYY-MM-DD"                           # ±12 h window (default)
python run_dr.py "YYYY-MM-DD" --hours 24                # wider window
python run_dr.py "YYYY-MM-DD HH:MM" "YYYY-MM-DD HH:MM"  # explicit start/end
python run_dr.py "YYYY-MM-DD" "YYYY-MM-DD"              # whole days, end day included
```

This saves `dr_conditions.md` and prints `Output folder: <path>` on its last line. Use that
path as `<out_dir>`: a day goes to `outputs/dr_YYYYMMDD/`, an explicit window to
`outputs/dr_<start>_<end>/`, so two reports starting the same day do not overwrite each other.

## Step B — multi-agent report generation (DR-only)

3-agent pipeline — spawn agents **sequentially** (each waits for the previous), with `<out_dir>` = the printed output folder, and `<name>` = its last part (e.g. `dr_20250218`):

1. **`dr-analyst`** — prompt: `<out_dir> = <path>`. Writes `extracted_dr.md` (reads `docs/dr_physics_reference.md` itself).
2. **`report-writer`** — prompt: `<out_dir> = <path>. This is a DR-only report (<name>).` Writes `[UNSIGNED] <name>.md` using the "DR-only report sections" structure in `docs/report_style_guide.md`.
3. **`critic`** — prompt: `<out_dir> = <path>. This is a DR-only report.` Uses its DR-only checklist. If any item fails, one revision pass with `report-writer` in revision mode.

## Step C — upload to LabArchives

```bash
cd $PROJECT_ROOT
python upload_to_labarchives.py <out_dir>
```
