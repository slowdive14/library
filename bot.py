import os
import json
import logging
import time
import gspread
import asyncio
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import http.server
import threading
from bucheon_library import BucheonLibraryClient, SEARCH_PAGE_URL, describe_holdings, is_available

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_CREDENTIALS = os.getenv("GOOGLE_SHEET_CREDENTIALS")
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL")
MONITOR_INTERVAL_MINUTES = int(os.getenv("MONITOR_INTERVAL_MINUTES", "5"))

# Bucheon library codes (from API)
BUCHEON_LIBRARIES = {
    "141321": "상동도서관",
    "141535": "원미도서관",
    "141043": "심곡도서관",
    "141056": "북부도서관",
    "141065": "꿈빛도서관",
    "141115": "책마루도서관",
    "141151": "한울빛도서관",
    "141248": "꿈여울도서관",
    "141559": "송내도서관",
    "141584": "오정도서관",
    "141583": "도당도서관",
    "141315": "동화도서관",
    "141603": "역곡도서관",
    "141652": "별빛마루도서관",
    "141651": "수주도서관",
    "141660": "역곡밝은도서관",
}

DEFAULT_LIB_CODE = "141652"
DEFAULT_LIB_NAME = "별빛마루도서관"

# Sheet column holding the last seen availability (Y/N). Render's disk is wiped
# on every restart, so the state lives in the sheet instead of status.json.
STATUS_HEADER = "Status"

# Warn on Telegram after this many monitor passes in a row where every lookup failed
FAILED_PASSES_BEFORE_WARNING = 3


class SheetManager:
    """Manages Google Sheet operations."""
    def __init__(self):
        self.sheet = None
        self._connect()

    def _connect(self):
        try:
            json_str = GOOGLE_SHEET_CREDENTIALS
            if not json_str:
                logger.error("GOOGLE_SHEET_CREDENTIALS is empty")
                return

            # Robust JSON parsing (handles potential newline issues from environment variables/secrets)
            try:
                creds_dict = json.loads(json_str)
            except json.JSONDecodeError:
                # Fix for common formatting issues in private_key
                import re
                def fix_newlines(match):
                    return match.group(0).replace('\n', '\\n')
                json_str = re.sub(r'"private_key"\s*:\s*"[^"]*"', fix_newlines, json_str, flags=re.DOTALL)
                creds_dict = json.loads(json_str)

            # Normalize private_key format
            if 'private_key' in creds_dict:
                pk = creds_dict['private_key']
                pk = pk.replace('\\n', '\n')
                creds_dict['private_key'] = pk

            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            client = gspread.authorize(creds)
            self.sheet = client.open_by_url(GOOGLE_SHEET_URL).sheet1
            logger.info("Successfully connected to Google Sheets")
        except Exception as e:
            logger.error(f"Failed to connect to Google Sheet: {e}")

    def get_all_books(self):
        """Returns all books from the sheet."""
        if not self.sheet:
            return []
        try:
            return self.sheet.get_all_records()
        except Exception as e:
            logger.error(f"Error reading sheet: {e}")
            return []

    def add_book(self, title, lib_code=DEFAULT_LIB_CODE, lib_name=DEFAULT_LIB_NAME, isbn=""):
        """Adds a book to the sheet."""
        if not self.sheet:
            return False
        try:
            self.sheet.append_row([title, lib_code, lib_name, isbn])
            return True
        except Exception as e:
            logger.error(f"Error adding book: {e}")
            return False

    def delete_book(self, title):
        """Deletes a book from the sheet by title."""
        if not self.sheet:
            return False
        try:
            records = self.sheet.get_all_records()
            for i, row in enumerate(records, start=2):  # Start from row 2 (after header)
                if row.get('Title', '').strip().lower() == title.strip().lower():
                    self.sheet.delete_rows(i)
                    return True
            return False
        except Exception as e:
            logger.error(f"Error deleting book: {e}")
            return False

    def update_statuses(self, changes):
        """Writes availability (Y/N) to the Status column.

        changes: list of (title, lib_code, status). Rows are matched by title and
        library code at write time, so a /d during a check can't shift the writes.
        """
        if not self.sheet or not changes:
            return
        try:
            headers = self.sheet.row_values(1)
            if STATUS_HEADER in headers:
                col = headers.index(STATUS_HEADER) + 1
            else:
                col = len(headers) + 1
                if col > self.sheet.col_count:
                    self.sheet.add_cols(col - self.sheet.col_count)
                self.sheet.update_cell(1, col, STATUS_HEADER)

            wanted = {(title, lib_code): status for title, lib_code, status in changes}
            for i, row in enumerate(self.sheet.get_all_records(), start=2):
                key = (str(row.get('Title', '')).strip(), str(row.get('LibraryCode', '')).strip())
                if key in wanted:
                    self.sheet.update_cell(i, col, wanted[key])
        except Exception as e:
            logger.error(f"Error updating statuses: {e}")


# Global clients (initialized in main)
lib_client = None
sheet_manager = None


def start_health_server():
    """Starts a dummy HTTP server for Render health checks."""
    port = int(os.environ.get("PORT", 8443))

    class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def do_HEAD(self):
            # Some uptime pingers use HEAD instead of GET
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            return  # Disable logging for health checks

    try:
        server = http.server.HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        logger.info(f"Health check server started on port {port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Failed to start health check server on port {port}: {e}")
        # In Render, failing the port bind is fatal, but we log it for debugging


async def send_telegram_notification(application: Application, message: str):
    """Send a notification via Telegram."""
    try:
        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.info("Telegram notification sent.")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


def run_monitor_pass():
    """Checks every monitored book once (blocking; run in a worker thread).

    Returns (notifications, changes, checked, failed). A book is announced when
    it goes from N (or never seen) to Y, as described in logic.md.
    """
    notifications, changes = [], []
    checked = failed = 0

    for row in sheet_manager.get_all_books():
        title = str(row.get('Title', '')).strip()
        lib_code = str(row.get('LibraryCode', '')).strip()
        if not title or not lib_code:
            continue
        lib_name = row.get('LibraryName') or BUCHEON_LIBRARIES.get(lib_code, lib_code)
        isbn = str(row.get('ISBN', '')).strip()

        checked += 1
        holdings = lib_client.find_holdings(lib_code, title, isbn)
        if holdings is None:
            failed += 1
            logger.warning(f"Could not check availability for '{title}' @ {lib_name}")
            continue

        saved_status = str(row.get(STATUS_HEADER, '')).strip()
        status = 'Y' if is_available(holdings) else 'N'
        logger.info(f"{title} @ {lib_name}: {status} (Last: {saved_status or 'N'})")

        if (saved_status or 'N') == 'N' and status == 'Y':
            available_isbn = next(h['isbn'] for h in holdings if is_available([h]))
            notifications.append(
                f"📚 대출 가능!\n\n📖 {title}\n📍 {lib_name}\n\n🔗 {SEARCH_PAGE_URL}{available_isbn}"
            )
        if saved_status != status:
            changes.append((title, lib_code, status))

        # Be nice to the library server
        time.sleep(1)

    return notifications, changes, checked, failed


async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job: notify books that became available and save the new statuses."""
    if not sheet_manager or not sheet_manager.sheet:
        logger.error("Monitor skipped: Google Sheet is not connected")
        return

    try:
        notifications, changes, checked, failed = await asyncio.to_thread(run_monitor_pass)
    except Exception as e:
        logger.error(f"Monitor pass failed: {e}")
        return

    for message in notifications:
        await send_telegram_notification(context.application, message)
    if changes:
        await asyncio.to_thread(sheet_manager.update_statuses, changes)

    # Every lookup failing means the catalog is down or its API changed
    if checked and failed == checked:
        context.bot_data['failed_passes'] = context.bot_data.get('failed_passes', 0) + 1
        if context.bot_data['failed_passes'] == FAILED_PASSES_BEFORE_WARNING:
            await send_telegram_notification(
                context.application,
                f"⚠️ 도서관 조회가 {FAILED_PASSES_BEFORE_WARNING}회 연속 실패했습니다.\n"
                "도서관 사이트가 점검 중이거나 검색 API가 바뀌었을 수 있습니다."
            )
    else:
        context.bot_data['failed_passes'] = 0


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help message."""
    help_text = f"""📚 부천 도서관 봇 명령어

/s 책제목 - 책 대출 가능 여부 조회
/st - 모니터링 중인 책들 현재 상태
/l - 모니터링 목록 보기
/a 책제목 - 모니터링에 책 추가
/d 책제목 - 모니터링에서 책 제거
/h - 이 도움말 보기

⏰ 자동 모니터링: {MONITOR_INTERVAL_MINUTES}분마다 체크
📡 부천시립도서관 검색 시스템의 실시간 정보입니다"""
    await update.message.reply_text(help_text)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search for a book and check availability at all Bucheon libraries."""
    if not context.args:
        await update.message.reply_text("사용법: /s 책제목\n또는: /s ISBN번호")
        return

    query = ' '.join(context.args)
    logger.info(f"Command /s received with query: {query}")

    # Check if query is ISBN (13 digits)
    if query.replace('-', '').isdigit() and len(query.replace('-', '')) == 13:
        isbn = query.replace('-', '')
        await check_book_by_isbn(update, isbn)
        return

    # Immediate feedback
    status_msg = await update.message.reply_text(f"🔍 '{query}' 검색 중...")

    copies = await asyncio.to_thread(lib_client.search, query, None, "SCORE")
    if copies is None:
        await status_msg.edit_text("❌ 도서관 검색 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
        return

    # The catalog returns one entry per copy; group them into distinct books
    books = {}
    for copy in copies:
        if copy['isbn']:
            books.setdefault(copy['isbn'], copy)

    if not books:
        await status_msg.edit_text(f"❌ '{query}' 검색 결과가 없습니다. (부천시 도서관 미소장)")
        return

    # If only 1 result, check directly
    if len(books) == 1:
        await check_book_by_isbn(update, next(iter(books)))
        return

    # Show multiple results for user to choose
    response = f"📚 '{query}' 검색 결과 ({len(books)}건)\n\n"
    for i, book in enumerate(list(books.values())[:5], 1):
        title = book['title'][:40]
        author = book['author'][:20]
        response += f"{i}. {title}\n   👤 {author}\n   /isbn{book['isbn']}\n\n"

    response += "👆 원하는 책의 /isbn... 클릭"
    await status_msg.edit_text(response)


async def check_book_by_isbn(update: Update, isbn: str):
    """Check book availability by ISBN at every Bucheon library."""
    copies = await asyncio.to_thread(lib_client.search, isbn)
    if copies is None:
        await update.message.reply_text("❌ 도서관 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
        return
    copies = [c for c in copies if c['isbn'] == isbn]

    by_library = {}
    for copy in copies:
        by_library.setdefault(copy['libCode'], []).append(copy)

    available_libs = []
    unavailable_libs = []
    for lib_code, lib_copies in by_library.items():
        lib_name = BUCHEON_LIBRARIES.get(lib_code, lib_copies[0]['libName'])
        icon, detail = describe_holdings(lib_copies)
        if icon == "✅":
            available_libs.append(lib_name)
        else:
            unavailable_libs.append(f"{lib_name} ({detail})")

    title = copies[0]['title'] if copies else f"ISBN {isbn}"
    author = copies[0]['author'] if copies else ""

    response = f"📖 {title}\n"
    if author:
        response += f"👤 {author}\n"
    response += f"🔢 ISBN: {isbn}\n\n"

    if available_libs:
        response += "✅ 대출 가능:\n"
        for lib in available_libs:
            response += f"  • {lib}\n"

    if unavailable_libs:
        response += "\n❌ 대출 중:\n"
        for lib in unavailable_libs:
            response += f"  • {lib}\n"

    if not available_libs and not unavailable_libs:
        response += "📭 부천시 도서관에 소장하지 않음"

    # Add library website link for verification (use ISBN for accuracy)
    response += f"\n\n🔗 실제 확인: {SEARCH_PAGE_URL}{isbn}"

    await update.message.reply_text(response)


async def cmd_isbn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /isbn{number} commands."""
    text = update.message.text
    # Extract ISBN from command like /isbn9788931039560
    if text.startswith('/isbn'):
        isbn = text[5:].strip()
        if isbn and len(isbn) == 13 and isbn.isdigit():
            await check_book_by_isbn(update, isbn)
        else:
            await update.message.reply_text("잘못된 ISBN입니다.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check status of all monitored books."""
    logger.info("Command /st received")
    books = sheet_manager.get_all_books()

    if not books:
        await update.message.reply_text("📭 모니터링 중인 책이 없습니다.")
        return

    await update.message.reply_text(f"🔍 {len(books)}권 상태 확인 중...")

    results = []
    for row in books:
        title = str(row.get('Title', '')).strip()
        lib_code = str(row.get('LibraryCode', DEFAULT_LIB_CODE))
        lib_name = row.get('LibraryName', DEFAULT_LIB_NAME)
        isbn = str(row.get('ISBN', '')).strip()

        if not title:
            continue

        holdings = await asyncio.to_thread(lib_client.find_holdings, lib_code, title, isbn)
        if holdings is None:
            results.append(f"❓ {title} @ {lib_name}")
            continue

        icon, detail = describe_holdings(holdings)
        line = f"{icon} {title} @ {lib_name}"
        if icon == "❌":
            line += f"\n    {detail}"
        results.append(line)

    response = "📚 **모니터링 상태**\n\n" + "\n".join(results)
    response += "\n\n✅=대출가능 ❌=대출불가 📭=미소장 ❓=조회실패"
    await update.message.reply_text(response)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all monitored books."""
    books = sheet_manager.get_all_books()

    if not books:
        await update.message.reply_text("📭 모니터링 중인 책이 없습니다.")
        return

    lines = []
    for i, row in enumerate(books, 1):
        title = row.get('Title', '제목 없음')
        lib_name = row.get('LibraryName', '도서관 미지정')
        lines.append(f"{i}. {title} @ {lib_name}")

    response = "📚 **모니터링 목록**\n\n" + "\n".join(lines)
    await update.message.reply_text(response)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a book to monitoring list."""
    logger.info("Command /a received")
    if not context.args:
        await update.message.reply_text("사용법: /추가 책제목")
        return

    title = ' '.join(context.args)
    status_msg = await update.message.reply_text(f"📝 '{title}' 모니터링 추가 중...")

    try:
        # No ISBN is stored: the monitor matches copies by title, so it isn't
        # tied to whichever edition a search happened to return first
        if not await asyncio.to_thread(sheet_manager.add_book, title):
            await status_msg.edit_text(f"❌ 추가 실패. 브라우저에서 직접 시트에 추가하거나 나중에 다시 시도해주세요.")
            return

        note = ""
        holdings = await asyncio.to_thread(lib_client.find_holdings, DEFAULT_LIB_CODE, title)
        if holdings is not None:
            icon, detail = describe_holdings(holdings)
            note = f"\n\n현재 {DEFAULT_LIB_NAME}: {icon} {detail}"
            if not holdings:
                note += "\n⚠️ 도서관에 등록된 제목과 다르면 알림이 오지 않습니다. /s 로 정확한 제목을 확인해주세요."
        await status_msg.edit_text(f"✅ '{title}' 모니터링 목록에 추가했습니다.{note}")
    except Exception as e:
        logger.error(f"Add error: {e}")
        await status_msg.edit_text(f"❌ 처리 중 오류가 발생했습니다: {e}")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a book from monitoring list."""
    if not context.args:
        await update.message.reply_text("사용법: /삭제 책제목")
        return

    title = ' '.join(context.args)

    if sheet_manager.delete_book(title):
        await update.message.reply_text(f"✅ '{title}' 모니터링 목록에서 삭제했습니다.")
    else:
        await update.message.reply_text(f"❌ '{title}'을(를) 찾을 수 없습니다.")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages as book search."""
    text = update.message.text.strip()
    logger.info(f"Received plain text message: {text}")
    if not text:
        return

    # Treat plain text as a book search
    context.args = text.split()
    await cmd_search(update, context)


def main():
    """Start the bot."""
    global lib_client, sheet_manager
    logger.info("Starting Telegram Bot...")

    # Polling is the default so the health server can answer an external uptime
    # pinger with 200 and keep Render's free instance (and the monitor job) awake.
    # USE_WEBHOOK=true brings back webhook mode, which binds the port itself.
    use_webhook = os.environ.get('USE_WEBHOOK', '').lower() == 'true'
    if (os.environ.get('RENDER') or os.environ.get('PORT')) and not use_webhook:
        threading.Thread(target=start_health_server, daemon=True).start()
        logger.info("Background health check server thread started")

    # Initialize clients safely
    try:
        lib_client = BucheonLibraryClient()
        sheet_manager = SheetManager()
    except Exception as e:
        logger.error(f"Critical error during initialization: {e}")
        # Don't exit yet, so Render doesn't loop crash, but bot won't work correctly

    # Create the Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("h", cmd_help))
    application.add_handler(CommandHandler("search", cmd_search))
    application.add_handler(CommandHandler("s", cmd_search))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("st", cmd_status))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("l", cmd_list))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("a", cmd_add))
    application.add_handler(CommandHandler("delete", cmd_delete))
    application.add_handler(CommandHandler("d", cmd_delete))
    application.add_handler(CommandHandler("start", cmd_help))

    # Handle /isbn{number} commands
    application.add_handler(MessageHandler(filters.Regex(r'^/isbn\d{13}$'), cmd_isbn))

    # Handle plain text as book search (must be last)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Background availability monitor
    if application.job_queue is None:
        logger.error('JobQueue unavailable: install "python-telegram-bot[job-queue]" to enable monitoring')
    else:
        application.job_queue.run_repeating(monitor_job, interval=MONITOR_INTERVAL_MINUTES * 60, first=30)
        logger.info(f"Availability monitor scheduled every {MONITOR_INTERVAL_MINUTES} minutes")

    # Run the bot
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    port = int(os.environ.get("PORT", 8443))

    if use_webhook and render_url:
        # Webhook mode: Telegram messages wake Render up, but the monitor only runs while awake
        logger.info(f"Running in webhook mode on port {port}")
        logger.info(f"External URL: {render_url}")
        
        # Webhook URL must be HTTPS
        webhook_url = f"{render_url}/{TELEGRAM_BOT_TOKEN}"
        
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES
        )
    else:
        # Polling mode (Render default and local development).
        # Starting polling deletes any webhook left over from webhook mode.
        logger.info("Running in polling mode")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
