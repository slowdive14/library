# Bucheon Library Smart Monitor

Automated system to check book availability in Bucheon libraries and send Telegram notifications when a book becomes available.

## 🚀 Setup Guide

### 1. Google Sheets Setup
1. Create a new Google Sheet.
2. Rename the first sheet to `Sheet1` (default).
3. Create the following headers in the first row:
    - **Title** (Required): Exact title of the book.
    - **LibraryCode** (Required): The code of the library to check (e.g., `141001`).
    - **LibraryName** (Optional): A human-readable name for the library.
    - **ISBN** (Optional): If provided, saves an API search call.
4. **Share** the sheet with the `client_email` from your Google Service Account (see below) giving it **Editor** access.

### 2. API Keys & Secrets
You need to obtain the following keys and save them as **GitHub Secrets** (Settings -> Secrets and variables -> Actions -> New repository secret).

| Secret Name | Description | How to get it |
| --- | --- | --- |
| `LIBRARY_API_KEY` | Library Info Naru API Key | Apply at [data4library.kr](https://www.data4library.kr/) |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | Create a new bot with [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your Chat ID | Message your bot, then get ID via `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `GOOGLE_SHEET_CREDENTIALS` | Service Account JSON | Google Cloud Console -> IAM & Admin -> Service Accounts -> Create Key (JSON). **Copy the entire JSON content.** |
| `GOOGLE_SHEET_URL` | Full URL of your Sheet | Copy from browser address bar. |

### 3. Running Locally (Testing)
1. Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
2. Set environment variables (e.g., in a `.env` file or export in terminal).
3. Run the script:
    ```bash
    python monitor.py
    ```

### 4. GitHub Actions (Automation)
The system is configured to run **every hour**.
- The workflow file is located at `.github/workflows/monitor.yml`.
- It will automatically commit a `status.json` file to the repo to track the previous state of book availability.

## 📂 Project Structure
- `monitor.py`: Main logic script.
- `.github/workflows/monitor.yml`: Automation schedule.
- `requirements.txt`: Python dependencies.
