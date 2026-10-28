import os
import re
import sys
import logging
from logging.handlers import RotatingFileHandler
import time
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
import http.server
import threading
from bucheon_library import (BucheonLibraryClient, KST, SEARCH_PAGE_URL, describe_holdings,
                             is_available, match_tier)
from isbn_barcode import decode_isbn
from sheets import SheetManager, STATUS_HEADER, book_key, short_key

# Load environment variables
load_dotenv()

# Configure logging. LOG_FILE also writes the log to a rotating file; under
# pythonw (e.g. Task Scheduler) there is no console, so it defaults to bot.log.
LOG_FILE = os.getenv("LOG_FILE")
running_windowless = sys.stderr is None or os.path.basename(sys.executable).lower() == "pythonw.exe"
if not LOG_FILE and running_windowless:
    LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
log_handlers = []
if sys.stderr is not None:
    log_handlers.append(logging.StreamHandler())
if LOG_FILE:
    log_handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=1, encoding='utf-8'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=log_handlers or None)
# httpx logs every Telegram API request URL at INFO, and that URL contains the bot token
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_CREDENTIALS = os.getenv("GOOGLE_SHEET_CREDENTIALS")
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL")
MONITOR_INTERVAL_MINUTES = int(os.getenv("MONITOR_INTERVAL_MINUTES", "5"))
NEW_ARRIVAL_HOURS = int(os.getenv("NEW_ARRIVAL_HOURS", "6"))
# Hours (KST) with no checks or alerts, e.g. "22-8"; empty turns it off. Libraries are
# closed then, and the first check afterwards still catches books returned overnight.
QUIET_HOURS = os.getenv("QUIET_HOURS", "22-8")

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

# LibraryCode values in the watch list besides a code or comma-separated codes
ALL_LIBRARIES = "ALL"     # every Bucheon library (what 🔔 and /a register)
HOME_LIBRARIES = "HOME"   # the 내 도서관 set chosen with /home

# Warn on Telegram after this many monitor passes in a row where every lookup failed
FAILED_PASSES_BEFORE_WARNING = 3

# /s search: copies fetched from the catalog (one entry per copy, so common
# titles need a few hundred) and distinct books listed in the reply
SEARCH_COPIES = 300
MAX_SEARCH_RESULTS = 30
TELEGRAM_MESSAGE_LIMIT = 4096

# New-arrival alerts sent per subscription per check; the rest are summarized
MAX_NEW_ARRIVAL_ALERTS = 5

# ISBN-13, or ISBN-10 (older books in the catalog, check digit may be X)
ISBN_PATTERN = r'\d{13}|\d{9}[\dX]'


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


async def send_telegram_notification(application: Application, message: str, reply_markup=None):
    """Send a notification via Telegram."""
    try:
        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, reply_markup=reply_markup)
        logger.info("Telegram notification sent.")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


# --- Libraries ------------------------------------------------------------

def short_name(lib_code, fallback=""):
    """'141652' -> '별빛마루' (catalog names of small libraries are already short)."""
    name = BUCHEON_LIBRARIES.get(str(lib_code), fallback or str(lib_code))
    return name.removesuffix("도서관") or name


def home_libraries():
    """내 도서관 codes from the Settings tab (default: 별빛마루). Blocking on first use."""
    value = sheet_manager.get_setting("home_libraries", "") if sheet_manager else ""
    codes = [c.strip() for c in str(value).split(",") if c.strip()]
    return codes or [DEFAULT_LIB_CODE]


def resolve_libraries(lib_code):
    """Library codes a watch-list row covers; None means every library."""
    code = str(lib_code).strip().upper()
    if code == ALL_LIBRARIES:
        return None
    if code == HOME_LIBRARIES:
        return home_libraries()
    return [c.strip() for c in code.split(",") if c.strip()]


def library_label(lib_code, row_name=""):
    code = str(lib_code).strip().upper()
    if code == ALL_LIBRARIES:
        return "🌐 부천 전체 도서관"
    if code == HOME_LIBRARIES:
        return "🏠 내 도서관(" + "·".join(short_name(c) for c in home_libraries()) + ")"
    return row_name or BUCHEON_LIBRARIES.get(code, code)


def group_by_library(copies, home=()):
    """libCode -> copies, 내 도서관 first, then libraries with an available copy."""
    groups = {}
    for copy in copies:
        groups.setdefault(copy['libCode'], []).append(copy)
    order = sorted(groups, key=lambda code: (code not in home, not is_available(groups[code])))
    return {code: groups[code] for code in order}


def home_first(holdings, home):
    """Copies reordered so 내 도서관 comes first (e.g. in '대출가능 (별빛마루, …)')."""
    return sorted(holdings, key=lambda h: h['libCode'] not in home)


def copy_location(copy):
    """'별빛마루 · 종합자료실(2층) · 181.71-슈192ㅂ'"""
    parts = [short_name(copy['libCode'], copy['libName']), copy['shelf'], copy['callNo']]
    return " · ".join(p for p in parts if p)


def in_quiet_hours(now=None):
    if not QUIET_HOURS.strip():
        return False
    try:
        start, end = (int(x) for x in QUIET_HOURS.split("-"))
    except ValueError:
        logger.warning(f"Ignoring malformed QUIET_HOURS={QUIET_HOURS!r} (expected e.g. 22-8)")
        return False
    hour = (now or datetime.now(KST)).hour
    return (start <= hour or hour < end) if start > end else (start <= hour < end)


def display_title(title):
    """Title without its subtitle, for the watch list."""
    return re.split(r'\s*[:=]\s*', title)[0].strip() or title


def chunk(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


# --- Availability monitor -------------------------------------------------

def format_available_alert(title, holdings):
    """Alert text listing where each available copy is: library, 자료실, 청구기호."""
    home = home_libraries()
    available = group_by_library([h for h in holdings if is_available([h])], home)
    lines = [f"📚 대출 가능!\n\n📖 {title}"]
    for code, copies in list(available.items())[:3]:
        mark = "🏠 " if code in home else "📍 "
        lines.append(mark + copy_location(copies[0]))
    if len(available) > 3:
        lines.append(f"…외 {len(available) - 3}곳")
    first_isbn = next(iter(available.values()))[0]['isbn']
    lines.append(f"🔗 {SEARCH_PAGE_URL}{first_isbn}")
    return "\n".join(lines[:1]) + "\n\n" + "\n".join(lines[1:-1]) + "\n\n" + lines[-1]


def alert_buttons(key):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 빌렸어요", callback_data=f"got:{key}"),
        InlineKeyboardButton("🔕 이번엔 패스", callback_data=f"skip:{key}"),
    ]])


def run_monitor_pass():
    """Checks every monitored book once (blocking; run in a worker thread).

    Returns (notifications, changes, checked, failed); notifications are
    (text, row key) pairs. A book is announced when it goes from N (or never
    seen) to Y, as described in logic.md.
    """
    notifications, changes = [], []
    checked = failed = 0
    sheet_manager.reload_settings()

    for row in sheet_manager.get_all_books():
        title = str(row.get('Title', '')).strip()
        lib_code = str(row.get('LibraryCode', '')).strip()
        if not title or not lib_code:
            continue
        label = library_label(lib_code, row.get('LibraryName'))
        isbn = str(row.get('ISBN', '')).strip()

        checked += 1
        holdings = lib_client.find_holdings(resolve_libraries(lib_code), title, isbn)
        if holdings is None:
            failed += 1
            logger.warning(f"Could not check availability for '{title}' @ {label}")
            continue

        saved_status = str(row.get(STATUS_HEADER, '')).strip()
        status = 'Y' if is_available(holdings) else 'N'
        logger.info(f"{title} @ {label}: {status} (Last: {saved_status or 'N'})")

        if (saved_status or 'N') == 'N' and status == 'Y':
            notifications.append((format_available_alert(title, holdings), book_key(row)))
        if saved_status != status:
            changes.append((title, lib_code, status))

        # Be nice to the library server
        time.sleep(1)

    return notifications, changes, checked, failed


async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job: notify books that became available and save the new statuses."""
    if in_quiet_hours():
        return
    if not sheet_manager or not sheet_manager.sheet:
        logger.error("Monitor skipped: Google Sheet is not connected")
        return

    try:
        notifications, changes, checked, failed = await asyncio.to_thread(run_monitor_pass)
    except Exception as e:
        logger.error(f"Monitor pass failed: {e}")
        return

    for message, key in notifications:
        await send_telegram_notification(context.application, message, alert_buttons(key))
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


# --- New-arrival alerts ---------------------------------------------------

def find_matching_books(query):
    """Books whose title or author matches a subscription, newest arrival first.

    Returns ISBN -> copies, or None when the catalog lookup failed (blocking).
    """
    copies = lib_client.search(query, None, "RECENT", SEARCH_COPIES)
    if copies is None:
        return None
    books = {}
    for copy in copies:
        if copy['isbn'] and match_tier(query, copy['title'], copy['author']) is not None:
            books.setdefault(copy['isbn'], []).append(copy)
    return books


def format_new_arrival(query, copies):
    book = copies[0]
    arrived = max(c['inputDate'] for c in copies)
    return (f"🆕 신착 도서 · '{query}'\n\n📖 {book['title']}\n👤 {book['author']}\n"
            f"📅 입고 {arrived}\n{summarize_libraries(copies, home_libraries())}")


def book_buttons(isbn):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📍 도서관별 현황", callback_data=f"d:{isbn}"),
        InlineKeyboardButton("🔔 알림 받기", callback_data=f"w:{isbn}"),
    ]])


def format_isbn_arrival(copies, new_codes, first_arrival):
    """Alert for an ISBN subscription: the book reached Bucheon, or more libraries."""
    book = copies[0]
    head = "🆕 기다리던 책이 부천 도서관에 들어왔어요!" if first_arrival else "🆕 다른 도서관에도 새로 들어왔어요"
    arrived = group_by_library([c for c in copies if c['libCode'] in new_codes], home_libraries())
    lines = [library_line(code, cs) for code, cs in arrived.items()]
    if len(lines) > 5:
        lines = lines[:5] + [f"… 외 {len(lines) - 5}곳"]
    return f"{head}\n\n📖 {book['title']}\n👤 {book['author']}\n\n" + "\n".join(lines)


def run_new_arrival_pass():
    """Returns [(text, isbn or None)] for books new since the last check (blocking)."""
    alerts = []
    for sub in sheet_manager.get_subscriptions():
        if sub['ISBN']:
            # One exact book: Seen holds the libraries that already have it
            copies = find_isbn_copies(sub['ISBN'])
            if copies is None:
                continue
            holding = {c['libCode'] for c in copies}
            new_codes = holding - sub['Seen']
            if new_codes:
                alerts.append((format_isbn_arrival(copies, new_codes, not sub['Seen']), sub['ISBN']))
                sheet_manager.save_subscription(sub['Query'], sub['Seen'] | holding, sub['ISBN'])
            time.sleep(1)
            continue

        books = find_matching_books(sub['Query'])
        if books is None:
            continue
        new = [isbn for isbn in books if isbn not in sub['Seen']]
        for isbn in new[:MAX_NEW_ARRIVAL_ALERTS]:
            alerts.append((format_new_arrival(sub['Query'], books[isbn]), isbn))
        if len(new) > MAX_NEW_ARRIVAL_ALERTS:
            alerts.append((f"🆕 '{sub['Query']}' 신착 도서가 {len(new) - MAX_NEW_ARRIVAL_ALERTS}권 더 있어요. "
                           f"/s {sub['Query']} 로 확인해 보세요.", None))
        if new:
            sheet_manager.save_subscription(sub['Query'], sub['Seen'] | set(books))
        time.sleep(1)
    return alerts


async def new_arrival_job(context: ContextTypes.DEFAULT_TYPE):
    if in_quiet_hours() or not sheet_manager or not sheet_manager.spreadsheet:
        return
    try:
        alerts = await asyncio.to_thread(run_new_arrival_pass)
    except Exception as e:
        logger.error(f"New-arrival check failed: {e}")
        return
    for text, isbn in alerts:
        await send_telegram_notification(context.application, text, book_buttons(isbn) if isbn else None)


# --- Commands -------------------------------------------------------------

BOT_COMMANDS = [
    BotCommand("s", "책 검색 (제목·저자·ISBN)"),
    BotCommand("st", "알림 걸어 둔 책 상태"),
    BotCommand("l", "알림 목록 (대출·신착) · 해제"),
    BotCommand("a", "제목으로 알림 추가"),
    BotCommand("new", "신착 알림 (저자·키워드·ISBN)"),
    BotCommand("home", "내 도서관 설정"),
    BotCommand("h", "도움말"),
]


async def post_init(application: Application):
    """Registers the command menu shown when typing '/'."""
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        logger.error(f"Failed to set bot commands: {e}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help message."""
    quiet = f"\n🌙 {QUIET_HOURS}시에는 확인과 알림을 쉬어요" if QUIET_HOURS.strip() else ""
    help_text = f"""📚 부천 도서관 봇

🔍 책 제목이나 저자를 그냥 보내면 검색해요
📷 책 뒷면 바코드 사진을 보내도 돼요

/s 책제목 - 검색 (번호를 누르면 도서관별 현황)
/st - 알림 걸어 둔 책 상태
/l - 걸어 둔 알림 목록: 🔔 대출 알림 + 🆕 신착 알림 (🗑 버튼으로 해제)
/a 책제목 - 제목으로 알림 추가
/d 번호 또는 책제목 - 알림 삭제
/new 저자나 키워드 - 신착 알림
/new ISBN (또는 /new 제목 ISBN) - 그 책만 정확히 신착 알림
/home - 내 도서관 설정
/h - 이 도움말

⏰ 부천 전체 도서관을 {MONITOR_INTERVAL_MINUTES}분마다 확인해서 대출 가능해지면 알려요{quiet}
📡 부천시립도서관 검색 시스템의 실시간 정보입니다"""
    await update.message.reply_text(help_text)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search for a book and check availability at all Bucheon libraries."""
    if not context.args:
        await update.message.reply_text("사용법: /s 책제목\n또는: /s ISBN번호")
        return

    query = ' '.join(context.args)
    logger.info(f"Command /s received with query: {query}")

    # Check if query is an ISBN
    isbn = query.replace('-', '').upper()
    if re.fullmatch(ISBN_PATTERN, isbn):
        await show_book(update.message, isbn)
        return

    # Immediate feedback
    status_msg = await update.message.reply_text(f"🔍 '{query}' 검색 중...")

    # Title search finds the book itself; keyword search adds books by that author
    title_copies, keyword_copies, home = await asyncio.gather(
        asyncio.to_thread(lib_client.search, query, None, "SCORE", SEARCH_COPIES, "title"),
        asyncio.to_thread(lib_client.search, query, None, "SCORE", SEARCH_COPIES),
        asyncio.to_thread(home_libraries),
    )
    if title_copies is None and keyword_copies is None:
        await status_msg.edit_text("❌ 도서관 검색 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
        return

    books = rank_search_results(query, title_copies or [], keyword_copies or [])
    if not books:
        await status_msg.edit_text(
            f"❌ '{query}' 검색 결과가 없습니다.\n"
            "제목이나 저자가 일치하는 책이 부천시 도서관에 없어요. "
            "띄어쓰기를 바꾸거나 제목 일부만 입력해 보세요."
        )
        return

    # If only 1 result, check directly
    if len(books) == 1:
        await status_msg.delete()
        await show_book(update.message, next(iter(books)))
        return

    # Show multiple results for user to choose
    shown = list(books.values())[:MAX_SEARCH_RESULTS]
    blocks = [f"📚 '{query}' 검색 결과 ({len(books)}건)"]
    for i, book_copies in enumerate(shown, 1):
        book = book_copies[0]
        blocks.append(
            f"{i}. {book['title'][:40]}\n   👤 {book['author'][:20]}\n"
            f"   {summarize_libraries(book_copies, home)}"
        )

    footer = "👇 번호를 누르면 도서관별 현황과 🔔 알림 버튼이 나와요"
    if len(books) > MAX_SEARCH_RESULTS:
        footer = (f"… 외 {len(books) - MAX_SEARCH_RESULTS}건은 생략했습니다. "
                  f"제목을 더 구체적으로 입력해 주세요.\n\n{footer}")
    blocks.append(footer)

    numbers = InlineKeyboardMarkup(chunk(
        [InlineKeyboardButton(str(i), callback_data=f"d:{c[0]['isbn']}") for i, c in enumerate(shown, 1)], 6))
    messages = split_message(blocks)
    await status_msg.edit_text(messages[0], reply_markup=numbers if len(messages) == 1 else None)
    for i, message in enumerate(messages[1:], 2):
        await update.message.reply_text(message, reply_markup=numbers if i == len(messages) else None)


def rank_search_results(query, title_copies, keyword_copies):
    """Groups catalog copies into books (ISBN -> copies), best matches first.

    Books from the title search are always kept. Keyword search also matches
    subjects, series and subtitles, so books only it found are kept only when
    match_tier accepts their title or author. Ties go to books held by more
    libraries (usually the edition people mean), then to catalog order.
    """
    candidates = {}  # isbn -> [copies, found by title search, first-seen order]
    for from_title, copies in ((True, title_copies), (False, keyword_copies)):
        grouped = {}
        for copy in copies:
            if copy['isbn']:
                grouped.setdefault(copy['isbn'], []).append(copy)
        for isbn, book_copies in grouped.items():
            if isbn not in candidates:
                candidates[isbn] = [book_copies, from_title, len(candidates)]
            elif len(book_copies) > len(candidates[isbn][0]):
                candidates[isbn][0] = book_copies

    ranked = []
    for isbn, (book_copies, from_title, order) in candidates.items():
        tier = match_tier(query, book_copies[0]['title'], book_copies[0]['author'])
        if tier is None:
            if not from_title:
                continue
            tier = 4
        holding = len({c['libCode'] for c in book_copies})
        ranked.append((tier, -holding, order, isbn))
    ranked.sort()
    return {isbn: candidates[isbn][0] for *_, isbn in ranked}


def summarize_libraries(copies, home=()):
    """One book's copies across libraries, e.g. '✅ 대출가능 3곳 / 소장 7곳 · 🏠 별빛마루 ✅'."""
    holding = {c['libCode'] for c in copies}
    available = {c['libCode'] for c in copies if is_available([c])}
    if available:
        text = f"✅ 대출가능 {len(available)}곳 / 소장 {len(holding)}곳"
    else:
        text = f"❌ 대출가능 없음 / 소장 {len(holding)}곳"
    if home:
        marks = [f"{short_name(code)} {'✅' if code in available else '❌'}" for code in home if code in holding]
        text += " · 🏠 " + (", ".join(marks) if marks else "미소장")
    return text


def split_message(blocks, limit=TELEGRAM_MESSAGE_LIMIT):
    """Joins blocks with blank lines into as few messages as fit Telegram's length limit."""
    messages, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > limit:
            messages.append(current)
            current = block
        else:
            current = candidate
    messages.append(current)
    return messages


def find_isbn_copies(isbn):
    """Every copy of one ISBN across Bucheon libraries, or None on failure (blocking)."""
    copies = lib_client.search(isbn, None, "LOANABLE", SEARCH_COPIES)
    return None if copies is None else [c for c in copies if c['isbn'] == isbn]


def library_line(code, lib_copies):
    """'✅ 별빛마루 · 종합자료실(2층) · 181.71-슈192ㅂ' or '❌ 수주 · 대출중 · 반납예정 …'."""
    icon, detail = describe_holdings(lib_copies)
    if icon == "✅":
        return f"✅ {copy_location(next(c for c in lib_copies if is_available([c])))}"
    if any(c['reservable'] for c in lib_copies):
        detail += " · 예약 가능"
    return f"❌ {short_name(code, lib_copies[0]['libName'])} · {detail}"


def library_status_lines(holdings, home, limit=4):
    """Per-library lines for /st: where the book can be borrowed now (내 도서관 first),
    or, when every copy is out, each library by earliest return date."""
    by_library = group_by_library(holdings, home)
    available = [(code, cs) for code, cs in by_library.items() if is_available(cs)]
    if available:
        ordered = available
    else:
        def earliest_return(lib_copies):
            return min((c['returnPlanDate'] for c in lib_copies if c['returnPlanDate']), default="9999")
        ordered = sorted(by_library.items(), key=lambda item: (item[0] not in home, earliest_return(item[1])))
    lines = [library_line(code, cs) for code, cs in ordered[:limit]]
    if len(ordered) > limit:
        more = "대출가능 " if available else ""
        lines.append(f"… 외 {more}{len(ordered) - limit}곳")
    return lines


def format_book_detail(isbn, copies, home):
    """Text and buttons for one book's status at every library, 내 도서관 first."""
    if not copies:
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("🆕 부천 도서관에 들어오면 알림", callback_data=f"n:{isbn}")]])
        return f"📭 ISBN {isbn}\n아직 부천시 도서관에 없는 책이에요.", buttons

    book = copies[0]
    header = [f"📖 {book['title']}"]
    if book['author']:
        header.append(f"👤 {book['author']}")
    header.append(f"🔢 ISBN {isbn}")

    by_library = group_by_library(copies, home)
    home_lines = [f"  {library_line(code, by_library[code])}" if code in by_library
                  else f"  📭 {short_name(code)} · 소장하지 않음" for code in home]
    other_lines = [f"  {library_line(code, cs)}" for code, cs in by_library.items() if code not in home]

    sections = ["\n".join(header), "🏠 내 도서관\n" + "\n".join(home_lines)]
    if other_lines:
        sections.append("🌐 다른 도서관\n" + "\n".join(other_lines))

    # 상호대차: not available at 내 도서관, but another library can send its copy over
    home_available = any(is_available(by_library.get(code, [])) for code in home)
    lenders = [short_name(code, cs[0]['libName']) for code, cs in by_library.items()
               if code not in home and any(is_available([c]) and c['mutualLoan'] for c in cs)]
    if not home_available and lenders:
        more = f" 등 {len(lenders)}곳" if len(lenders) > 1 else ""
        sections.append(f"💡 상호대차: {lenders[0]}{more}의 책을 내 도서관에서 받아 볼 수 있어요. "
                        "도서관 홈페이지에서 신청하세요.")

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 대출 가능해지면 알림 받기", callback_data=f"w:{isbn}")],
        [InlineKeyboardButton("🔗 도서관 홈페이지에서 보기", url=f"{SEARCH_PAGE_URL}{isbn}")],
    ])
    return "\n\n".join(sections), buttons


async def show_book(message, isbn):
    """Replies with one book's status at every Bucheon library."""
    copies, home = await asyncio.gather(asyncio.to_thread(find_isbn_copies, isbn),
                                        asyncio.to_thread(home_libraries))
    if copies is None:
        await message.reply_text("❌ 도서관 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
        return
    text, buttons = format_book_detail(isbn, copies, home)
    await message.reply_text(text, reply_markup=buttons)


def register_watch(isbn):
    """Adds one ISBN to the watch list for every library; returns the reply text (blocking)."""
    copies = find_isbn_copies(isbn)
    if copies is None:
        return "❌ 도서관 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
    if not copies:
        return f"📭 아직 부천시 도서관에 없는 책이에요. 들어오면 알림을 받으려면 /new {isbn} 을 보내세요."

    title = display_title(copies[0]['title'])
    for row in sheet_manager.get_all_books():
        if str(row.get('ISBN', '')).strip() == isbn:
            return f"ℹ️ 이미 알림을 걸어 둔 책이에요.\n📖 {title}"
    if not sheet_manager.add_book(title, ALL_LIBRARIES, "전체", isbn):
        return "❌ 시트에 저장하지 못했어요. 잠시 후 다시 시도해주세요."
    return f"🔔 알림 등록 완료\n📖 {title}\n\n{watch_status_note(copies)}\n\n걸어 둔 알림은 /l 에서 볼 수 있어요."


def watch_status_note(holdings):
    """What happens next for a newly watched book, given its current copies (blocking)."""
    icon, detail = describe_holdings(home_first(holdings, home_libraries()), with_library=True)
    when = "다음 확인 때" if not in_quiet_hours() else f"아침({QUIET_HOURS.split('-')[1]}시) 첫 확인 때"
    if icon == "✅":
        return f"현재: ✅ {detail}\n{when} 자료실 위치와 함께 알림을 보내 드릴게요."
    if icon == "📭":
        return "현재: 📭 부천시 도서관에 없어요. 들어오면 알려 드릴게요."
    return f"현재: ❌ {detail}\n부천 어느 도서관에서든 대출 가능해지면 알려 드릴게요."


async def cmd_isbn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /isbn{number} commands."""
    text = update.message.text
    # Extract ISBN from command like /isbn9788931039560 or /isbn898371154X
    if text.startswith('/isbn'):
        isbn = text[5:].strip().upper()
        if re.fullmatch(ISBN_PATTERN, isbn):
            await show_book(update.message, isbn)
        else:
            await update.message.reply_text("잘못된 ISBN입니다.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check status of all monitored books."""
    logger.info("Command /st received")
    books = await asyncio.to_thread(sheet_manager.get_all_books)

    if not books:
        await update.message.reply_text("📭 알림을 걸어 둔 책이 없습니다.\n/s 로 검색해서 🔔 버튼으로 추가해 보세요.")
        return

    await update.message.reply_text(f"🔍 {len(books)}권 상태 확인 중...")

    home = await asyncio.to_thread(home_libraries)
    results = []
    for row in books:
        title = str(row.get('Title', '')).strip()
        lib_code = str(row.get('LibraryCode', DEFAULT_LIB_CODE))
        isbn = str(row.get('ISBN', '')).strip()
        if not title:
            continue

        # Rows that don't watch every library (e.g. added by older versions) show their scope
        all_libraries = lib_code.strip().upper() == ALL_LIBRARIES
        scope = "" if all_libraries else await asyncio.to_thread(library_label, lib_code, row.get('LibraryName'))
        name = f"{title} @ {scope}" if scope else title

        codes = await asyncio.to_thread(resolve_libraries, lib_code)
        holdings = await asyncio.to_thread(lib_client.find_holdings, codes, title, isbn)
        if holdings is None:
            results.append(f"❓ {name}")
            continue

        icon, _ = describe_holdings(holdings)
        line = f"{icon} {name}"
        if icon == "📭":
            line += f"\n    {scope or '부천 전체 도서관'}에 소장하지 않음"
        else:
            line += "".join(f"\n    {status}" for status in library_status_lines(holdings, home))
        results.append(line)

    response = "📚 알림 걸어 둔 책 상태\n\n" + "\n".join(results)
    response += "\n\n✅=대출가능 ❌=대출불가 📭=미소장 ❓=조회실패"
    for message in split_message([response]):
        await update.message.reply_text(message)


def describe_subscription(sub):
    """'📘 코스모스 (978…)\n    부천 26곳 소장 · …' for the alert list."""
    if sub['ISBN']:
        held = len(sub['Seen'])
        state = (f"이 책만 · 부천 {held}곳 소장, 다른 도서관에 들어오면 알림" if held
                 else "이 책만 · 아직 부천에 없음, 들어오면 알림")
        return f"📘 {sub['Query']}\n    {state}"
    return f"🔎 {sub['Query']}\n    제목이나 저자가 일치하는 새 책 (지금 {len(sub['Seen'])}권 있음)"


def build_alert_list():
    """Text and 🗑 buttons for /l and /new: 🔔 availability alerts and 🆕 new-arrival alerts (blocking)."""
    books = sheet_manager.get_all_books()
    subs = sheet_manager.get_subscriptions()
    if not books and not subs:
        return ("📭 걸어 둔 알림이 없습니다.\n"
                "🔔 대출 알림: /s 로 검색해서 🔔 버튼\n"
                "🆕 신착 알림: /new 저자나 키워드, /new ISBN"), None

    sections, rows = [], []
    if books:
        lines, buttons = [], []
        for i, row in enumerate(books, 1):
            title = str(row.get('Title', '')).strip() or '제목 없음'
            lines.append(f"{i}. {title} @ {library_label(row.get('LibraryCode', ''), row.get('LibraryName'))}")
            buttons.append(InlineKeyboardButton(f"🗑 {i}", callback_data=f"del:{book_key(row)}"))
        sections.append(f"🔔 대출 알림 ({MONITOR_INTERVAL_MINUTES}분마다 확인)\n" + "\n".join(lines))
        rows += chunk(buttons, 5)
    else:
        sections.append("🔔 대출 알림: 없음 (/s 로 검색해서 🔔 버튼으로 추가)")

    if subs:
        lines, buttons = [], []
        for i, sub in enumerate(subs, 1):
            lines.append(f"{i}. {describe_subscription(sub)}")
            buttons.append(InlineKeyboardButton(f"🗑 🆕{i}", callback_data=f"nd:{short_key(sub['Query'])}"))
        sections.append(f"🆕 신착 알림 ({NEW_ARRIVAL_HOURS}시간마다 확인)\n" + "\n".join(lines))
        rows += chunk(buttons, 5)
    else:
        sections.append("🆕 신착 알림: 없음 (/new 저자나 키워드, /new ISBN)")

    text = "📚 알림 목록\n\n" + "\n\n".join(sections) + "\n\n🗑 버튼을 누르면 그 알림을 해제합니다."
    return text, InlineKeyboardMarkup(rows)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List availability alerts and new-arrival alerts."""
    text, buttons = await asyncio.to_thread(build_alert_list)
    await update.message.reply_text(text, reply_markup=buttons)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a book to monitoring list."""
    logger.info("Command /a received")
    if not context.args:
        await update.message.reply_text("사용법: /a 책제목\n(검색 결과의 🔔 버튼으로 추가하면 판본까지 정확해요)")
        return

    title = ' '.join(context.args)
    status_msg = await update.message.reply_text(f"📝 '{title}' 알림 추가 중...")

    try:
        # No ISBN is stored: copies are matched by title in every library
        if not await asyncio.to_thread(sheet_manager.add_book, title, ALL_LIBRARIES, "전체"):
            await status_msg.edit_text(f"❌ 추가 실패. 브라우저에서 직접 시트에 추가하거나 나중에 다시 시도해주세요.")
            return

        note = ""
        holdings = await asyncio.to_thread(lib_client.find_holdings, None, title)
        if holdings is not None:
            note = "\n\n" + await asyncio.to_thread(watch_status_note, holdings)
            editions = {h['isbn'] for h in holdings}
            if not holdings:
                note += "\n⚠️ 도서관에 등록된 제목과 다르면 알림이 오지 않습니다. /s 로 정확한 제목을 확인해주세요."
            elif len(editions) > 1:
                note += (f"\nℹ️ 제목이 같은 책이 {len(editions)}종이라 모두 확인해요. "
                         "특정 책만 원하면 /s 로 검색해서 🔔 버튼으로 추가하세요.")
        await status_msg.edit_text(f"🔔 '{title}' 알림을 추가했습니다.{note}")
    except Exception as e:
        logger.error(f"Add error: {e}")
        await status_msg.edit_text(f"❌ 처리 중 오류가 발생했습니다: {e}")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a book from monitoring list by its /l number or title."""
    if not context.args:
        await update.message.reply_text("사용법: /d 번호 (/l 목록의 번호) 또는 /d 책제목")
        return

    arg = ' '.join(context.args)
    logger.info(f"Command /d received: {arg}")
    if arg.isdigit():
        books = await asyncio.to_thread(sheet_manager.get_all_books)
        index = int(arg)
        if not 1 <= index <= len(books):
            await update.message.reply_text(f"❌ {index}번 책이 없습니다. /l 로 번호를 확인해 주세요.")
            return
        deleted = await asyncio.to_thread(sheet_manager.delete_by_key, book_key(books[index - 1]))
    else:
        deleted = await asyncio.to_thread(sheet_manager.delete_book, arg)

    if deleted:
        await update.message.reply_text(f"✅ '{deleted if isinstance(deleted, str) else arg}' 알림을 삭제했습니다.")
    else:
        await update.message.reply_text(f"❌ '{arg}'을(를) 찾을 수 없습니다.")


def home_message(home):
    names = ", ".join(short_name(code) for code in home)
    text = (f"🏠 내 도서관: {names}\n\n"
            "검색 결과와 도서관별 현황, 알림에서 내 도서관을 맨 앞에 보여 줘요.\n"
            "알림은 내 도서관과 상관없이 부천 전체 도서관을 확인해요.")
    buttons = [InlineKeyboardButton(f"{'✅ ' if code in home else ''}{short_name(code)}", callback_data=f"h:{code}")
               for code in BUCHEON_LIBRARIES]
    rows = chunk(buttons, 4) + [[InlineKeyboardButton("완료", callback_data="h:done")]]
    return text, InlineKeyboardMarkup(rows)


async def cmd_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Choose 내 도서관 (shown first everywhere)."""
    home = await asyncio.to_thread(home_libraries)
    text, buttons = home_message(home)
    await update.message.reply_text(text + "\n\n아래에서 눌러서 바꿀 수 있어요.", reply_markup=buttons)


def subscribe_isbn(isbn, label=""):
    """New-arrival alert for one exact book; returns the reply text (blocking).

    If the book is already in Bucheon, alerts come when more libraries get it;
    otherwise the first arrival anywhere is announced.
    """
    if any(s['ISBN'] == isbn for s in sheet_manager.get_subscriptions()):
        return f"ℹ️ ISBN {isbn} 신착 알림은 이미 등록되어 있어요."
    copies = find_isbn_copies(isbn)
    if copies is None:
        return "❌ 도서관 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

    label = label or (display_title(copies[0]['title']) if copies else "")
    query = f"{label} ({isbn})" if label else f"ISBN {isbn}"
    holding = {c['libCode'] for c in copies}
    if not sheet_manager.save_subscription(query, holding, isbn):
        return "❌ 시트에 저장하지 못했어요. 잠시 후 다시 시도해주세요."

    if not copies:
        return (f"🆕 '{query}' 신착 알림을 등록했어요.\n"
                f"아직 부천시 도서관에 없는 책이에요. 어느 도서관에든 들어오면 알려 드릴게요 "
                f"({NEW_ARRIVAL_HOURS}시간마다 확인).\n\n걸어 둔 알림은 /l 에서 볼 수 있어요.")
    names = [short_name(code, cs[0]['libName']) for code, cs in group_by_library(copies, home_libraries()).items()]
    names = ", ".join(names[:5]) + (f" 외 {len(names) - 5}곳" if len(names) > 5 else "")
    return (f"🆕 '{query}' 신착 알림을 등록했어요.\n"
            f"지금 {len(holding)}곳에 있어요: {names}\n"
            "다른 도서관에 새로 들어오면 알려 드릴게요. 대출 가능 알림은 책 상세 화면의 🔔 버튼을 쓰세요.\n\n"
            "걸어 둔 알림은 /l 에서 볼 수 있어요.")


def parse_new_args(args):
    """'/new 제목 9791234567890' -> ('제목', '9791234567890'); no ISBN -> (text, '')."""
    isbn, words = "", []
    for word in args:
        candidate = word.replace('-', '').upper()
        if not isbn and re.fullmatch(ISBN_PATTERN, candidate):
            isbn = candidate
        else:
            words.append(word)
    return ' '.join(words).strip(), isbn


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/new: list subscriptions; /new <author or keyword>: subscribe;
    /new [title] <ISBN>: subscribe to that exact book."""
    query, isbn = parse_new_args(context.args)
    if not query and not isbn:
        text, buttons = await asyncio.to_thread(build_alert_list)
        await update.message.reply_text(text, reply_markup=buttons)
        return

    logger.info(f"Command /new received: {query} {isbn}".strip())
    if isbn:
        status_msg = await update.message.reply_text(f"🔍 ISBN {isbn} 신착 알림 등록 중...")
        await status_msg.edit_text(await asyncio.to_thread(subscribe_isbn, isbn, query))
        return
    status_msg = await update.message.reply_text(f"🔍 '{query}' 신착 알림 등록 중...")
    subs = await asyncio.to_thread(sheet_manager.get_subscriptions)
    if any(s['Query'] == query for s in subs):
        await status_msg.edit_text(f"ℹ️ '{query}' 신착 알림은 이미 등록되어 있어요.")
        return

    books = await asyncio.to_thread(find_matching_books, query)
    if books is None:
        await status_msg.edit_text("❌ 도서관 검색 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
        return
    if not await asyncio.to_thread(sheet_manager.save_subscription, query, set(books)):
        await status_msg.edit_text("❌ 시트에 저장하지 못했어요. 잠시 후 다시 시도해주세요.")
        return

    text = (f"🆕 '{query}' 신착 알림을 등록했어요.\n"
            f"지금 있는 책 {len(books)}권은 빼고, 새로 들어오는 책만 {NEW_ARRIVAL_HOURS}시간마다 확인해서 알려 드릴게요.")
    if books:
        recent = [f"• {c[0]['title'][:30]} (입고 {max(x['inputDate'] for x in c)})" for c in list(books.values())[:3]]
        text += "\n\n최근 입고:\n" + "\n".join(recent)
    else:
        text += "\n\n지금은 제목이나 저자가 일치하는 책이 없어요. 오타가 없는지 확인해 주세요."
    text += "\n\n걸어 둔 알림은 /l 에서 볼 수 있어요."
    await status_msg.edit_text(text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A photo (or image file) of a book's barcode: show that book."""
    message = update.message
    source = message.photo[-1] if message.photo else message.document
    status_msg = await message.reply_text("📷 바코드 읽는 중...")
    try:
        file = await source.get_file()
        data = bytes(await file.download_as_bytearray())
    except Exception as e:
        logger.error(f"Photo download failed: {e}")
        await status_msg.edit_text("❌ 사진을 받지 못했어요. 다시 보내 주세요.")
        return

    isbn = await asyncio.to_thread(decode_isbn, data)
    if not isbn:
        await status_msg.edit_text(
            "❌ 바코드를 읽지 못했어요.\n"
            "책 뒷면의 ISBN 바코드가 크고 선명하게 나오도록 다시 찍어 주세요. "
            "그래도 안 되면 사진을 '파일'로 보내 보세요."
        )
        return
    logger.info(f"Barcode photo decoded: {isbn}")
    await status_msg.edit_text(f"📷 ISBN {isbn}")
    await show_book(message, isbn)


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages as book search."""
    text = update.message.text.strip()
    logger.info(f"Received plain text message: {text}")
    if not text:
        return

    # Treat plain text as a book search
    context.args = text.split()
    await cmd_search(update, context)


# --- Buttons --------------------------------------------------------------

async def edit_message(query, text, reply_markup=None):
    """Edits the message a button belongs to, ignoring 'message is not modified'."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes inline button presses by their callback data prefix."""
    query = update.callback_query
    action, _, arg = (query.data or "").partition(":")
    logger.info(f"Button pressed: {action}")

    if action == "d":      # search result number / 📍 도서관별 현황
        await query.answer()
        await show_book(query.message, arg)

    elif action == "w":    # 🔔 알림 받기
        await query.answer("알림 등록 중...")
        await query.message.reply_text(await asyncio.to_thread(register_watch, arg))

    elif action == "n":    # 🆕 부천 도서관에 들어오면 알림 (book not in Bucheon yet)
        await query.answer("신착 알림 등록 중...")
        await query.message.reply_text(await asyncio.to_thread(subscribe_isbn, arg))

    elif action == "got":  # ✅ 빌렸어요 on an availability alert
        deleted = await asyncio.to_thread(sheet_manager.delete_by_key, arg)
        await query.answer()
        note = "✅ 빌렸어요 · 알림 목록에서 뺐어요." if deleted else "ℹ️ 이미 알림 목록에 없는 책이에요."
        await edit_message(query, f"{query.message.text}\n\n{note}")

    elif action == "skip":  # 🔕 이번엔 패스
        await query.answer()
        await edit_message(query, f"{query.message.text}\n\n🔕 이번엔 패스 · 대출됐다가 다시 반납되면 또 알려 드릴게요.")

    elif action == "del":  # 🗑 on a 🔔 alert in /l
        deleted = await asyncio.to_thread(sheet_manager.delete_by_key, arg)
        await query.answer("삭제했어요" if deleted else "이미 삭제된 책이에요")
        text, buttons = await asyncio.to_thread(build_alert_list)
        await edit_message(query, text, buttons)

    elif action == "h":    # /home library toggles
        home = await asyncio.to_thread(home_libraries)
        if arg == "done":
            await query.answer()
            text, _ = home_message(home)
            await edit_message(query, text)
            return
        home = [c for c in home if c != arg] if arg in home else home + [arg]
        if not home:
            await query.answer("최소 한 곳은 골라야 해요", show_alert=True)
            return
        saved = await asyncio.to_thread(sheet_manager.set_setting, "home_libraries", ",".join(home))
        await query.answer("저장했어요" if saved else "저장하지 못했어요")
        text, buttons = home_message(home)
        await edit_message(query, text + "\n\n아래에서 눌러서 바꿀 수 있어요.", buttons)

    elif action == "nd":   # 🗑 on a 🆕 alert in /l
        removed = await asyncio.to_thread(sheet_manager.delete_subscription, arg)
        await query.answer(f"'{removed}' 해제" if removed else "이미 해제됐어요")
        text, buttons = await asyncio.to_thread(build_alert_list)
        await edit_message(query, text, buttons)

    else:
        await query.answer()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.callback_query:
        try:
            await update.callback_query.answer("처리 중 오류가 발생했어요. 잠시 후 다시 눌러 주세요.", show_alert=True)
        except Exception:
            pass


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
        sheet_manager = SheetManager(GOOGLE_SHEET_CREDENTIALS, GOOGLE_SHEET_URL)
    except Exception as e:
        logger.error(f"Critical error during initialization: {e}")
        # Don't exit yet, so Render doesn't loop crash, but bot won't work correctly

    # Create the Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

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
    application.add_handler(CommandHandler("new", cmd_new))
    application.add_handler(CommandHandler("home", cmd_home))
    application.add_handler(CommandHandler("start", cmd_help))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_error_handler(on_error)

    # Handle /isbn{number} commands
    application.add_handler(MessageHandler(filters.Regex(rf'(?i)^/isbn({ISBN_PATTERN})$'), cmd_isbn))

    # Barcode photos, sent as a photo or as an image file
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))

    # Handle plain text as book search (must be last)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Background jobs: availability monitor and new-arrival alerts
    if application.job_queue is None:
        logger.error('JobQueue unavailable: install "python-telegram-bot[job-queue]" to enable monitoring')
    else:
        application.job_queue.run_repeating(monitor_job, interval=MONITOR_INTERVAL_MINUTES * 60, first=30)
        application.job_queue.run_repeating(new_arrival_job, interval=NEW_ARRIVAL_HOURS * 3600, first=120)
        logger.info(f"Availability monitor scheduled every {MONITOR_INTERVAL_MINUTES} minutes, "
                    f"new arrivals every {NEW_ARRIVAL_HOURS} hours, quiet hours {QUIET_HOURS or 'off'}")

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
    try:
        main()
    except Exception:
        # Without a console the traceback would be lost; Task Scheduler restarts the bot
        logger.exception("Bot stopped by an unhandled error")
        raise
