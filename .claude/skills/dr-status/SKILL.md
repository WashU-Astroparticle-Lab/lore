---
name: dr-status
description: Report the CURRENT dilution refrigerator status — no date given, or phrased as "right now", "currently", "how's the DR", "what's the MXC temp". Read-only quick check — runs the parser for today with a short window and replies in Slack with a brief plain-text status. Does NOT write or upload a report. For a report over a specific past date/window use dr-report instead.
---

# DR Quick Status (no report)

Use when the user asks about **current** DR conditions. Read `lab_config.md` (per CLAUDE.md) for `$PROJECT_ROOT`. Do **not** write a report or upload anything — just run the parser and reply in Slack with a short plain-text status.

## Steps

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
