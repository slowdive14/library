# Bucheon Library Smart Monitor

Telegram bot that checks book availability in Bucheon libraries and sends a notification when a monitored book becomes available.

Loan status comes from the Bucheon library catalog (`alpasq.bcl.go.kr`), the same real-time data the official search site shows. It replaced the Library Info Naru (data4library) API, whose data lagged about a day behind. The catalog API is undocumented: if every lookup fails three checks in a row, the bot sends a warning on Telegram.

## 🚀 Setup Guide

### 1. Google Sheets Setup
1. Create a new Google Sheet.
2. Rename the first sheet to `Sheet1` (default).
3. Create the following headers in the first row:
    - **Title** (Required): Title of the book. Copies are matched by main title (subtitle, spaces and punctuation are ignored).
    - **LibraryCode** (Required): The code of the library to check (e.g., `141652`).
    - **LibraryName** (Optional): A human-readable name for the library.
    - **ISBN** (Optional): Tried first; if that library has no copy with this ISBN, the title is used.
    - **Status**: Written by the bot (`Y`/`N`, last seen availability). Added automatically if missing.
4. **Share** the sheet with the `client_email` from your Google Service Account (see below) giving it **Editor** access.

### 2. Environment Variables
Set these in Render (Environment) or in a local `.env` file.

| Name | Description | How to get it |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | Create a new bot with [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your Chat ID | Message your bot, then get ID via `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `GOOGLE_SHEET_CREDENTIALS` | Service Account JSON | Google Cloud Console -> IAM & Admin -> Service Accounts -> Create Key (JSON). **Copy the entire JSON content.** |
| `GOOGLE_SHEET_URL` | Full URL of your Sheet | Copy from browser address bar. |
| `MONITOR_INTERVAL_MINUTES` | Optional, default `5` | Minutes between availability checks. |
| `USE_WEBHOOK` | Optional | `true` switches Render back to webhook mode (see below). |

### 3. Running Locally (Testing)
1. Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
2. Set environment variables (e.g., in a `.env` file or export in terminal).
3. Run the bot:
    ```bash
    python bot.py
    ```
    ⚠️ Stop the Render service first. Only one bot instance can poll Telegram at a time, and starting polling removes the webhook if one is set.

### 4. Render Deployment (Automation)
`Procfile` runs `python bot.py`. The bot polls Telegram and checks every monitored book every `MONITOR_INTERVAL_MINUTES` minutes.

Render's free instance sleeps after 15 minutes without inbound requests, which also stops the monitor. Keep it awake with an external pinger such as [cron-job.org](https://cron-job.org) or [UptimeRobot](https://uptimerobot.com):
- URL: your Render URL (`https://<service>.onrender.com/`), which answers `200 OK`
- Interval: every 10 minutes

With `USE_WEBHOOK=true` the bot uses webhook mode instead: a Telegram message wakes the instance, but the webhook URL contains the bot token and cannot be pinged, so the monitor only runs while the instance happens to be awake.

## 📂 Project Structure
- `bot.py`: Telegram commands and the periodic availability monitor.
- `bucheon_library.py`: Client for the Bucheon library catalog (real-time loan status).
- `logic.md`: When a notification is sent.
- `requirements.txt`: Python dependencies.
