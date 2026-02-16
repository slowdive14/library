import os
import json
import logging
import requests
import gspread
import asyncio
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import http.server
import threading

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
LIBRARY_API_KEY = os.getenv("LIBRARY_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_CREDENTIALS = os.getenv("GOOGLE_SHEET_CREDENTIALS")
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL")

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

STATUS_FILE = "status.json"

class LibraryClient:
    """Interacts with the Library Information Naru API."""
    BASE_URL = "http://data4library.kr/api"

    def __init__(self, api_key):
        self.api_key = api_key

    def search_book(self, title):
        """Searches for a book by title and returns info."""
        # Try with original title first, then without spaces
        for search_title in [title, title.replace(' ', '')]:
            params = {
                'authKey': self.api_key,
                'title': search_title,
                'format': 'json',
                'pageSize': 5
            }
            try:
                response = requests.get(f"{self.BASE_URL}/srchBooks", params=params, timeout=10)
                response.raise_for_status()
                data = response.json()
                if data.get('response', {}).get('docs'):
                    return data['response']['docs']
            except Exception as e:
                logger.error(f"Error searching for book '{search_title}': {e}")
        return None

    def check_availability(self, lib_code, isbn13):
        """Checks if a book is available at a specific library."""
        params = {
            'authKey': self.api_key,
            'libCode': lib_code,
            'isbn13': isbn13,
            'format': 'json'
        }
        try:
            response = requests.get(f"{self.BASE_URL}/bookExist", params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            if 'response' in data and 'result' in data['response']:
                result = data['response']['result']
                return {
                    'hasBook': result.get('hasBook'),
                    'loanAvailable': result.get('loanAvailable')
                }
            return None
        except Exception as e:
            logger.error(f"Error checking availability: {e}")
            return None


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


class StateManager:
    """Manages the state of book availability."""
    @staticmethod
    def load_state():
        if os.path.exists(STATUS_FILE):
            try:
                with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load state: {e}")
        return {}

    @staticmethod
    def save_state(state):
        try:
            with open(STATUS_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")


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





async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help message."""
    help_text = """📚 부천 도서관 봇 명령어

/s 책제목 - 책 대출 가능 여부 조회
/st - 모니터링 중인 책들 현재 상태
/l - 모니터링 목록 보기
/a 책제목 - 모니터링에 책 추가
/d 책제목 - 모니터링에서 책 제거
/h - 이 도움말 보기

⏰ 자동 모니터링: 30분마다 체크
⚠️ API 데이터는 전날 기준입니다 (실시간 아님)"""
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
        await check_book_by_isbn(update, isbn, f"ISBN {isbn}")
        return

    # Immediate feedback
    status_msg = await update.message.reply_text(f"🔍 '{query}' 검색 중...")

    # Search for the book
    try:
        books = lib_client.search_book(query)
    except Exception as e:
        logger.error(f"Search error: {e}")
        await status_msg.edit_text(f"❌ 검색 중 오류가 발생했습니다: {e}")
        return

    if not books:
        await status_msg.edit_text(f"❌ '{query}' 검색 결과가 없습니다.")
        return

    # If only 1 result or user wants first, check directly
    if len(books) == 1:
        book = books[0]['doc']
        isbn = book.get('isbn13', '')
        await check_book_by_isbn(update, isbn, book.get('bookname', query), book.get('authors', ''))
        return

    # Show multiple results for user to choose
    response = f"📚 '{query}' 검색 결과 ({len(books)}건)\n\n"
    for i, b in enumerate(books[:5], 1):
        doc = b['doc']
        title = doc.get('bookname', '제목 없음')[:40]
        author = doc.get('authors', '')[:20]
        isbn = doc.get('isbn13', '')
        response += f"{i}. {title}\n   👤 {author}\n   /isbn{isbn}\n\n"

    response += "👆 원하는 책의 /isbn... 클릭"
    await status_msg.edit_text(response)


async def check_book_by_isbn(update: Update, isbn: str, title: str = "", author: str = ""):
    """Check book availability by ISBN."""
    import urllib.parse

    available_libs = []
    unavailable_libs = []

    for lib_code, lib_name in BUCHEON_LIBRARIES.items():
        availability = lib_client.check_availability(lib_code, isbn)
        if availability:
            if availability['hasBook'] == 'Y':
                if availability['loanAvailable'] == 'Y':
                    available_libs.append(lib_name)
                else:
                    unavailable_libs.append(lib_name)

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
    response += f"\n\n🔗 실제 확인: https://alpasq.bcl.go.kr/search/keyword/{isbn}"

    await update.message.reply_text(response)


async def cmd_isbn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /isbn{number} commands."""
    text = update.message.text
    # Extract ISBN from command like /isbn9788931039560
    if text.startswith('/isbn'):
        isbn = text[5:].strip()
        if isbn and len(isbn) == 13 and isbn.isdigit():
            await check_book_by_isbn(update, isbn, f"ISBN {isbn}")
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
        title = row.get('Title')
        lib_code = str(row.get('LibraryCode', DEFAULT_LIB_CODE))
        lib_name = row.get('LibraryName', DEFAULT_LIB_NAME)
        isbn = row.get('ISBN')

        if not title:
            continue

        # Get ISBN if not provided
        if not isbn:
            search_result = lib_client.search_book(title)
            if search_result:
                isbn = search_result[0]['doc'].get('isbn13', '')

        if isbn:
            availability = lib_client.check_availability(lib_code, isbn)
            if availability:
                if availability['hasBook'] == 'Y':
                    status = "✅" if availability['loanAvailable'] == 'Y' else "❌"
                else:
                    status = "📭"
            else:
                status = "❓"
        else:
            status = "❓"

        results.append(f"{status} {title} @ {lib_name}")

    response = "📚 **모니터링 상태**\n\n" + "\n".join(results)
    response += "\n\n✅=대출가능 ❌=대출중 📭=미소장"
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

    # Search for ISBN
    try:
        books = lib_client.search_book(title)
        isbn = ""
        if books:
            isbn = books[0]['doc'].get('isbn13', '')

        if sheet_manager.add_book(title, isbn=isbn):
            await status_msg.edit_text(f"✅ '{title}' 모니터링 목록에 추가했습니다.")
        else:
            await status_msg.edit_text(f"❌ 추가 실패. 브라우저에서 직접 시트에 추가하거나 나중에 다시 시도해주세요.")
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

    # Start health server in background for Render immediately
    # This helps Render detect the service as healthy as soon as possible
    if os.environ.get('RENDER') or os.environ.get('PORT'):
        threading.Thread(target=start_health_server, daemon=True).start()
        logger.info("Background health check server thread started")

    # Initialize clients safely
    try:
        lib_client = LibraryClient(LIBRARY_API_KEY)
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

    # Run the bot (always use Polling for simplicity with health server)
    # Render Free Tier supports both, but Polling + Health Server is more robust for Python
    logger.info("Running in polling mode with background health check")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
