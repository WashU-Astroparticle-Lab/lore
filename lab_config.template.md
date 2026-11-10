# Lab Configuration Template

Copy this file to `lab_config.md` and fill in your values. `lab_config.md` is gitignored.
The pipeline agent reads `lab_config.md` at startup — do not commit it.

## Project

| Key | Value |
|-----|-------|
| PROJECT_ROOT | /path/to/your/LORE/checkout |
| PYTHON | <absolute path to the interpreter that has the project's dependencies, e.g. C:/Users/you/miniconda3/python.exe> |
| Time zone | <the zone the fridge PC's clock is set to, as an IANA name, e.g. America/New_York; empty: this computer's own zone. The DR reader checks each log row's wall-clock time against it> |

## Required .env keys

The following keys must be present in `.env`. The agent checks these by name:

| Key | Purpose |
|-----|---------|
| GITHUB_TOKEN | GitHub personal access token (repo read access) |
| LA_AKID | LabArchives API key ID |
| LA_SECRET | LabArchives API secret |
| LA_UID | LabArchives user ID |
| DR_DATA_PATH | Local path to your dilution refrigerator .dat log directory |
| SLACK_BOT_TOKEN | Slack bot OAuth token (xoxb-...) |
| SLACK_APP_TOKEN | Slack app-level token for Socket Mode (xapp-...) |

## LabArchives

| Key | Value |
|-----|-------|
| Primary notebook | Your primary ELN notebook name |
| Other notebooks | Additional notebook names, comma-separated |
| Upload folder | Folder name inside the primary notebook where reports are uploaded |
| Wiring diagram page | Page title of your fridge wiring / RF attenuation diagram |
| SSO institution label | Exact institution name in the LabArchives login dropdown |
| SSO form prefix | CSS id prefix of your SSO username/password/submit fields, e.g. `#myLogin_` |

## Slack archive (optional)

Figures posted in these Slack channels are summarised by LORE and archived to LabArchives, one
page per week, in a subfolder of the upload folder (see `lab_agent/slack/archive.py`). Leave
the channels row empty to turn the archive off.

| Key | Value |
|-----|-------|
| Slack archive channels | Public channel names, comma-separated, e.g. `data_analysis, dr-cooldown` |
| Slack archive folder | Subfolder of the upload folder for the week pages, e.g. `Slack` |

## Security (optional)

| Key | Value |
|-----|-------|
| GitHub orgs | Comma-separated owner allowlist for GitHub URLs auto-discovered inside LabArchives pages (e.g. `WashU-Astroparticle-Lab`). Leave blank to allow any github.com owner. |
