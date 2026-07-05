# LORE

AI-powered lab assistant that fetches experiment data from GitHub and LabArchives, writes structured research reports with physics interpretation, and monitors dilution refrigerator conditions. Triggered via Slack.

---

## Usage

Once set up on the lab machine, everything is driven from Slack — no command line needed.

### Experiment report
Send the bot a GitHub experiment folder URL and your LabArchives page titles:
```
https://github.com/WashU-Astroparticle-Lab/<repo>/tree/main/<folder> "Page Title One" "Page Title Two"
```
The bot fetches all notebooks, lab notes, images, and data; writes a full prose report with physics interpretation; and uploads it to the **AI Agent** folder in LabArchives.

### Dilution refrigerator report
Ask the bot for a DR conditions report by date:
```
Give me a DR conditions report for Feb 18 2025
Give me a DR report for Feb 18 2025, 2pm–10pm
```
The bot parses the Leiden Cryogenics log files, writes a standalone cryogenic conditions report with physics interpretation, and uploads it to LabArchives.

### Quick DR status check
Ask the bot what the DR is doing right now:
```
How's the DR?
What's the MXC temp right now?
```
The bot reads the last 2 hours of Leiden Cryogenics data and replies directly in Slack — no report written.

### Vague or partial requests
The bot can search Slack history and LabArchives to fill in missing details. For example:
```
Write a report for the KID sweep Axel posted last Tuesday
```
The bot will search Slack for the GitHub URL and LabArchives page name before asking you to provide them.

---

## How it works

1. **`slack_listener.py`** — always-on Slack bot that receives messages and spawns a Claude Code session per request
2. **`run.py`** — fetches notebooks, lab notes, images, and CSV data from GitHub and LabArchives
3. **Claude Code** — reads the fetched files, writes the report, and asks whether to include DR conditions
4. **`upload_to_labarchives.py`** — converts the report to HTML and posts it to LabArchives
5. **`run_dr.py`** — parses Leiden Cryogenics `.dat` files for a given date/window

---

## Installation

### Prerequisites

- Python 3.11+ with [conda](https://docs.conda.io/) (`presto` environment)
- [Claude Code CLI](https://docs.anthropic.com/claude-code): `npm install -g @anthropic-ai/claude-code`
- A Slack app with Socket Mode enabled (no public URL needed)

### 1. Clone the repo

```bash
git clone https://github.com/<org>/lab-agent.git
cd lab-agent
```

### 2. Install dependencies

```bash
conda activate presto
pip install presto-2.16.0-py3-none-any.whl
pip install -r requirements.txt
playwright install chromium
```

### 3. Create `.env`

```
# GitHub
GITHUB_TOKEN=ghp_...

# LabArchives API
LA_AKID=Washington_StL_...
LA_SECRET=...                  # full password (~30 chars)
LA_UID=...

# LabArchives WashU SSO (for session cookie refresh)
LA_EMAIL_WU=you@wustl.edu
LA_PASSWORD_WU=...

# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# Dilution refrigerator (optional)
# Point at the Leiden Cryogenics Data/ folder — local or network path.
DR_DATA_PATH=C:/path/to/Leiden Cryogenics/Data
```

`LA_SESSION_COOKIE` is populated automatically by `get_la_cookies.py` — do not set it manually.

### 4. Log in to Claude Code

```bash
claude
```

Follow the browser OAuth flow. Credentials are saved locally and persist across sessions.

### 5. Seed LabArchives session cookies

```bash
python get_la_cookies.py
```

A browser window opens. Log in with your WashU credentials and approve the Duo MFA push. The script saves the cookie to `.env` automatically. Repeat this step whenever the pipeline reports expired cookies.

### 6. Start the Slack bot

```bash
conda activate presto
python slack_listener.py
```

To auto-start on Windows login, create a Task Scheduler task:
- **Program:** `C:\path\to\conda\Scripts\conda.exe`
- **Arguments:** `run -n presto python C:\path\to\lab-agent\slack_listener.py`
- **Start in:** `C:\path\to\lab-agent`

---

## Project structure

```
lab-agent/
├── CLAUDE.md                  # Instructions for the Claude Code agent
├── README.md                  # This file
├── requirements.txt
├── run.py                     # Fetches GitHub + LabArchives artifacts
├── run_dr.py                  # Parses Leiden Cryogenics .dat files
├── upload_to_labarchives.py   # Uploads reports to LabArchives
├── slack_listener.py          # Slack bot
├── get_la_cookies.py          # WashU SSO cookie refresh
├── presto-2.16.0-py3-none-any.whl
└── lab_agent/
    ├── models.py              # Data models
    ├── discover.py            # File classification + artifact discovery
    ├── ingest.py              # Notebook/CSV parsing
    ├── summarize.py           # Builds structured summary for Claude
    ├── dependencies.py        # Fetches lab-internal package source from GitHub
    ├── upload.py              # HTML conversion + image inlining for LabArchives
    ├── dr_conditions.py       # Leiden Cryogenics .dat parsing and thermal event detection
    └── sources/
        ├── github.py          # GitHub API client
        └── labarchives.py     # LabArchives API client + image downloader
```

---

## LabArchives

Reports are uploaded to the **AI Agent** folder inside the **Qubit & KID** notebook. Create that folder in LabArchives if it does not already exist.
