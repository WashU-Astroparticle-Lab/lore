# Lab Configuration Template

Copy this file to `lab_config.md` and fill in your values. `lab_config.md` is gitignored.
The pipeline agent reads `lab_config.md` at startup — do not commit it.

## Project

| Key | Value |
|-----|-------|
| PROJECT_ROOT | /path/to/your/LORE/checkout |

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
