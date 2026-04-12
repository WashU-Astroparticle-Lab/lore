# Lab Agent — Experiment Report Pipeline

Automated experiment report generation for the WashU Astroparticle Lab.
Given a GitHub experiment folder and one or more LabArchives page titles, fetches all
artifacts, writes a structured physics report, and uploads it to LabArchives.

Triggered via Slack (DM or @mention) or run manually from the command line.

---

## How it works

1. **`run.py`** — fetches notebooks, lab notes, images, and data from GitHub + LabArchives and saves them to `outputs/<experiment_id>/`
2. **Claude Code** — reads those files and writes a full report (`[UNSIGNED] <id>.md`)
3. **`upload_to_labarchives.py`** — converts the report to HTML and posts it to the *AI Agent* folder in the *Qubit & KID* LabArchives notebook
4. **`slack_listener.py`** — always-on Slack bot that handles the above pipeline end-to-end when triggered by a message containing a GitHub URL

---

## Prerequisites

- Python 3.11+
- [conda](https://docs.conda.io/) with a `presto` environment (used by the daq hardware library)
- [Claude Code CLI](https://docs.anthropic.com/claude-code) installed globally: `npm install -g @anthropic-ai/claude-code`
- A Slack workspace where you can install apps (for the bot)

---

## One-time setup

### 1. Clone the repo

```bash
git clone https://github.com/<org>/lab-agent-single-report.git
cd lab-agent-single-report
```

### 2. Activate the conda environment

```bash
conda activate presto
```

### 3. Install presto (not on PyPI — bundled wheel)

```bash
pip install presto-2.16.0-py3-none-any.whl
```

### 4. Install all other dependencies

```bash
pip install -r requirements.txt
```

### 5. Install Playwright's Chromium browser (for cookie refresh)

```bash
playwright install chromium
```

### 6. Create `.env` with your credentials

Copy the template and fill in each value:

```
GITHUB_TOKEN=ghp_...
LA_AKID=Washington_StL_...
LA_SECRET=...
LA_UID=...
LA_EMAIL_WU=you@wustl.edu
LA_PASSWORD_WU=...
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

`LA_SESSION_COOKIE` is populated automatically by `get_la_cookies.py` — you do not need to set it manually.

### 7. Log in to Claude Code (one-time per machine)

```bash
claude
```

Follow the browser OAuth flow. The CLI saves credentials locally and stays authenticated.

---

## Running the Slack listener

```bash
conda activate presto
python slack_listener.py
```

The bot listens over Socket Mode (no public URL needed). Send it a GitHub URL + LabArchives page names in a DM or @mention and it runs the full pipeline.

**To run it in the background / auto-start on Windows:**

Use Windows Task Scheduler. Create a task that runs on login:

- Program: `C:\path\to\conda\Scripts\conda.exe`
- Arguments: `run -n presto python C:\path\to\lab-agent-single-report\slack_listener.py`
- Start in: `C:\path\to\lab-agent-single-report`

---

## Running the pipeline manually

```bash
conda activate presto
python run.py "https://github.com/WashU-Astroparticle-Lab/<repo>/tree/main/<folder>" \
              "Page Title One" "Page Title Two"
```

Then read the output files in `outputs/<experiment_id>/` and write the report, or let Claude do it:

```bash
claude
# Then follow the CLAUDE.md pipeline instructions
```

Upload to LabArchives:

```bash
python upload_to_labarchives.py outputs/<experiment_id>
```

---

## Refreshing LabArchives session cookies

If the pipeline reports expired cookies:

```bash
python get_la_cookies.py
```

A browser window opens. Log in with your WashU credentials and approve the Duo MFA push.
The script saves the new cookie to `.env` automatically.

---

## Project structure

```
lab-agent-single-report/
├── CLAUDE.md                  # Pipeline instructions for Claude Code
├── README.md                  # This file
├── requirements.txt
├── .gitignore
├── run.py                     # Pipeline runner (fetch artifacts)
├── upload_to_labarchives.py   # Upload report to LabArchives
├── slack_listener.py          # Slack bot
├── get_la_cookies.py          # WashU SSO cookie refresh
├── presto-2.16.0-py3-none-any.whl  # presto wheel (not on PyPI)
└── lab_agent/                 # Core pipeline package
    ├── models.py              # Pydantic data models
    ├── discover.py            # File classification + artifact discovery
    ├── ingest.py              # Notebook/CSV parsing
    ├── summarize.py           # Bundle → StructuredSummary
    ├── dependencies.py        # Lab-specific import resolution from GitHub
    ├── upload.py              # LabArchives upload logic
    └── sources/
        ├── github.py          # GitHub artifact fetcher
        └── labarchives.py     # LabArchives page fetcher + image downloader
```

---

## LabArchives configuration

Reports are uploaded to the **AI Agent** folder inside the **Qubit & KID** notebook.
Create that folder in LabArchives if it does not already exist.
