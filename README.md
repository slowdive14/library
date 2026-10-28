# Bucheon Library Smart Monitor

Telegram bot that checks book availability in Bucheon libraries and sends a notification when a monitored book becomes available.

Loan status comes from the Bucheon library catalog (`alpasq.bcl.go.kr`), the same real-time data the official search site shows. It replaced the Library Info Naru (data4library) API, whose data lagged about a day behind. The catalog API is undocumented: if every lookup fails three checks in a row, the bot sends a warning on Telegram.

⚠️ The catalog drops connections from non-Korean IPs, so the bot must run on a host in Korea. Overseas hosts such as Render (US/EU/Singapore) or GitHub Actions time out.

## 💬 Using the Bot
- **Search**: send a title, an author or an ISBN (or `/s ...`). Results are ranked by title match and show how many libraries have a copy available, with 내 도서관 marked. Tap a number for the per-library status.
- **Barcode photo**: send a photo of a book's barcode (e.g. in a bookstore) to see its status in Bucheon libraries.
- **Per-library status**: 내 도서관 first, then every other library with 자료실 and 청구기호 for available copies, return dates and whether a hold (예약) is possible. When 내 도서관 has no copy available but another library can lend one, it suggests 상호대차.
- **🔔 Alerts**: the 🔔 button (or `/a 제목`) watches **every Bucheon library** and sends an alert when the book becomes available somewhere, listing where to find it (내 도서관 first). Tap **✅ 빌렸어요** to remove it from the list, or **🔕 이번엔 패스** to be told again the next time it comes back.
- **Watch list**: `/st` shows each book's status per library (where to find it, or return dates, holds, ⚠️ overdue), `/l` lists them with 🗑 buttons, `/d 번호` removes one.
- **New arrivals**: `/new 저자나 키워드` alerts you when a matching book arrives in any Bucheon library (checked every `NEW_ARRIVAL_HOURS`); `/new` alone lists them with 🗑 buttons.
- **New arrivals of one exact book**: `/new ISBN` (or `/new 제목 ISBN`) follows that ISBN only, so same-titled books can't trigger it. It alerts when the book first arrives in Bucheon, then whenever another library gets it. A book not in Bucheon yet (e.g. a barcode photo taken in a bookstore) shows a 🆕 button that does the same.
- **내 도서관**: `/home` chooses which libraries are shown first (default 별빛마루). It doesn't limit what alerts check.
- **Quiet hours**: no checks or alerts during `QUIET_HOURS` (default 22–8 KST); the first check afterwards catches anything returned overnight.

## 🚀 Setup Guide

### 1. Google Sheets Setup
1. Create a new Google Sheet.
2. Rename the first sheet to `Sheet1` (default).
3. Create the following headers in the first row:
    - **Title** (Required): Title of the book. Copies are matched by main title (subtitle, spaces and punctuation are ignored).
    - **LibraryCode** (Required): `ALL` (every library, what the bot adds), `HOME` (내 도서관), a library code (e.g. `141652`) or several codes separated by commas.
    - **LibraryName** (Optional): A human-readable name for the library.
    - **ISBN** (Optional): Tried first; if those libraries have no copy with this ISBN, the title is used.
    - **Status**: Written by the bot (`Y`/`N`, last seen availability). Added automatically if missing.
4. **Share** the sheet with the `client_email` from your Google Service Account (see below) giving it **Editor** access.

The bot also creates two tabs on first use: **Settings** (`home_libraries`) and **Subscriptions** (`Query`, `Seen`, `ISBN`: keyword subscriptions keep the ISBNs already announced, ISBN subscriptions the libraries already holding the book).

### 2. Environment Variables
Set these in a `.env` file in the project folder (on a VM, copy it there as described in section 5).

| Name | Description | How to get it |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | Create a new bot with [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your Chat ID | Message your bot, then get ID via `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `GOOGLE_SHEET_CREDENTIALS` | Service Account JSON | Google Cloud Console -> IAM & Admin -> Service Accounts -> Create Key (JSON). **Copy the entire JSON content.** |
| `GOOGLE_SHEET_URL` | Full URL of your Sheet | Copy from browser address bar. |
| `MONITOR_INTERVAL_MINUTES` | Optional, default `5` | Minutes between availability checks. |
| `NEW_ARRIVAL_HOURS` | Optional, default `6` | Hours between new-arrival checks for `/new`. |
| `QUIET_HOURS` | Optional, default `22-8` | Hours (KST, start-end) without checks or alerts. Empty turns it off. |
| `USE_WEBHOOK` | Optional | `true` uses webhook mode on hosts that set `RENDER_EXTERNAL_URL`. Not needed on a PC or VM. |
| `LOG_FILE` | Optional | Also write the log to this file (rotated at 5 MB). Defaults to `bot.log` when run with `pythonw`. |

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
    ⚠️ Stop any other running instance first (Windows task: `Disable-ScheduledTask -TaskName LibraryBot; Stop-ScheduledTask -TaskName LibraryBot`, VM: `sudo systemctl stop library-bot`). Only one bot instance can poll Telegram at a time, and starting polling removes the webhook if one is set.

### 4. Running 24/7 on a Windows PC (free)
A PC on a Korean home internet line can reach the catalog. Task Scheduler keeps the bot running in the background, with no console window, while you are signed in.

1. Put your `.env` in the project folder.
2. **Stop any other running instance** of the bot (e.g. suspend the old Render service). Two instances polling the same token keep failing with `Conflict` errors.
3. **Register the task** in the project folder (no admin needed). It creates `.venv` if missing, registers the `LibraryBot` task and starts it:
    ```powershell
    powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1
    ```
4. **Keep the PC awake**: Settings → System → Power → set sleep to **Never** when plugged in. Locking the screen is fine.
5. To come back after Windows Update restarts without you signing in, turn on Settings → Accounts → Sign-in options → **Use my sign-in info to automatically finish setting up after an update**.

The task fires every 5 minutes and is ignored while the bot runs, so the bot comes back within 5 minutes after sign-in, a crash or a startup without network.

| Task | Command (PowerShell, project folder) |
| --- | --- |
| Follow the log | `Get-Content bot.log -Wait -Tail 20 -Encoding UTF8` |
| Restart (e.g. after `git pull`) | `Stop-ScheduledTask -TaskName LibraryBot; Start-ScheduledTask -TaskName LibraryBot` |
| Update dependencies | `.venv\Scripts\python -m pip install -r requirements.txt` |
| Stop for good | `Disable-ScheduledTask -TaskName LibraryBot; Stop-ScheduledTask -TaskName LibraryBot` |
| Resume | `Enable-ScheduledTask -TaskName LibraryBot; Start-ScheduledTask -TaskName LibraryBot` |

### 5. Alternative: Azure VM in Korea Central
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
- `bot.py`: Telegram commands, buttons, the availability monitor and new-arrival alerts.
- `bucheon_library.py`: Client for the Bucheon library catalog (real-time loan status, title matching).
- `sheets.py`: Google Sheet storage (watch list, Settings and Subscriptions tabs).
- `isbn_barcode.py`: Reads the ISBN from a barcode photo (zxing-cpp).
- `deploy/windows/`: Task Scheduler setup for a Windows PC.
- `deploy/`: systemd unit and setup script for an Ubuntu VM in Korea.
- `logic.md`: When a notification is sent.
- `requirements.txt`: Python dependencies.
