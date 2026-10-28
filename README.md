# Bucheon Library Smart Monitor

Telegram bot that checks book availability in Bucheon libraries and sends a notification when a monitored book becomes available.

Loan status comes from the Bucheon library catalog (`alpasq.bcl.go.kr`), the same real-time data the official search site shows. It replaced the Library Info Naru (data4library) API, whose data lagged about a day behind. The catalog API is undocumented: if every lookup fails three checks in a row, the bot sends a warning on Telegram.

⚠️ The catalog drops connections from non-Korean IPs, so the bot must run on a host in Korea. Overseas hosts such as Render (US/EU/Singapore) or GitHub Actions time out.

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
Set these in a `.env` file (copied to the server in step 4).

| Name | Description | How to get it |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | Create a new bot with [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your Chat ID | Message your bot, then get ID via `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `GOOGLE_SHEET_CREDENTIALS` | Service Account JSON | Google Cloud Console -> IAM & Admin -> Service Accounts -> Create Key (JSON). **Copy the entire JSON content.** |
| `GOOGLE_SHEET_URL` | Full URL of your Sheet | Copy from browser address bar. |
| `MONITOR_INTERVAL_MINUTES` | Optional, default `5` | Minutes between availability checks. |
| `USE_WEBHOOK` | Optional | `true` uses webhook mode on hosts that set `RENDER_EXTERNAL_URL`. Not needed on a VM. |

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
    ⚠️ Stop the bot on the server first (`sudo systemctl stop library-bot`). Only one bot instance can poll Telegram at a time, and starting polling removes the webhook if one is set.

### 4. Deployment: Azure VM in Korea Central
The bot runs as a systemd service on an Ubuntu VM, polls Telegram and checks every monitored book every `MONITOR_INTERVAL_MINUTES` minutes. It needs no inbound ports besides SSH. Any Ubuntu VM with a Korean IP works the same way (e.g. Oracle Cloud with home region Chuncheon/Seoul; Tokyo, Osaka and Singapore are blocked).

1. **Create an [Azure free account](https://azure.microsoft.com/pricing/purchase-options/azure-account)**. For 12 months it includes 750 hours/month each of `B1s`, `B2pts v2` and `B2ats v2` VMs, enough for one VM running all month.
2. **Create a virtual machine** in the Azure portal
    - Region: **(Asia Pacific) Korea Central**
    - Image: Ubuntu Server 24.04 LTS - x64 Gen2
    - Size: `Standard_B1s` or `Standard_B2ats_v2` (shown as free services eligible)
    - Authentication: SSH public key, username `azureuser`; download the private key (`.pem`)
    - Inbound ports: SSH (22) only
    - Management tab: make sure **Auto-shutdown** is off
    - After creation, note the public IP.
3. **Clone the repo on the VM**
    ```bash
    ssh -i <private-key.pem> azureuser@<public-ip>
    git clone https://github.com/slowdive14/library.git ~/library
    exit
    ```
4. **Copy your `.env`** from your PC (run in the project folder):
    ```bash
    scp -i <private-key.pem> .env azureuser@<public-ip>:~/library/.env
    ```
5. **Stop any other running instance** of the bot (e.g. suspend the old Render service). Two instances polling the same token keep failing with `Conflict` errors.
6. **Install and start the service** on the VM:
    ```bash
    bash ~/library/deploy/setup.sh
    ```

Day-to-day commands on the VM:

| Task | Command |
| --- | --- |
| Follow logs | `journalctl -u library-bot -f` |
| Restart | `sudo systemctl restart library-bot` |
| Deploy new code | `cd ~/library && git pull && bash deploy/setup.sh` |

Costs to watch:
- **Upgrade to pay-as-you-go within 30 days.** The free account's subscription is disabled when the 30-day credit ends; only after upgrading do the 12 months of free services continue. See [Avoid charges with your Azure free account](https://learn.microsoft.com/azure/cost-management-billing/manage/avoid-charges-free-account).
- The VM hours are free, but other parts (OS disk, public IP, outbound traffic) may be billed separately. While the 30-day credit lasts, check **Cost Management → Cost analysis** to see what shows up, and add a small **budget alert** before upgrading.
- After 12 months the VM is billed at pay-as-you-go rates.

## 📂 Project Structure
- `bot.py`: Telegram commands and the periodic availability monitor.
- `bucheon_library.py`: Client for the Bucheon library catalog (real-time loan status).
- `deploy/`: systemd unit and setup script for an Ubuntu VM in Korea.
- `logic.md`: When a notification is sent.
- `requirements.txt`: Python dependencies.
