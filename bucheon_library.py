import html
import logging
import re
from datetime import datetime, timedelta, timezone
import requests

logger = logging.getLogger(__name__)

AVAILABLE = "대출가능"
SEARCH_PAGE_URL = "https://alpasq.bcl.go.kr/search/keyword/"

# Korea has no daylight saving time, so a fixed offset is exact (and needs no tzdata on Windows)
KST = timezone(timedelta(hours=9))


class BucheonLibraryClient:
    """Reads real-time loan status from the Bucheon library catalog (alpasq.bcl.go.kr).

    This is the JSON API the official search site calls. It is undocumented, so
    every method returns None when the request or the response format fails,
    letting callers tell "lookup failed" apart from "not in the library" ([]).
    """
    BASE_URL = "https://alpasq.bcl.go.kr/api"

    def __init__(self, timeout=10):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (compatible; bucheon-library-telegram-bot)',
        })
        self._manage_codes = None

    def _manage_code(self, lib_code):
        """Maps a library code (e.g. 141652) to the catalog's manageCode (e.g. AN)."""
        if self._manage_codes is None:
            try:
                response = self.session.get(f"{self.BASE_URL}/common/libraryInfo",
                                            params={'nationYn': 'N'}, timeout=self.timeout)
                response.raise_for_status()
                libs = response.json()['contents']['libList']
                self._manage_codes = {str(lib['libCode']): lib['manageCode'] for lib in libs}
            except Exception as e:
                logger.error(f"Error loading library list: {e}")
                return "ALL"
        return self._manage_codes.get(str(lib_code), "ALL")

    # Payload field per search scope: everything (title, subject, series...) or title only
    SEARCH_FIELDS = {'keyword': 'searchKeyword', 'title': 'advTitle'}

    def search(self, keyword, lib_code=None, order="LOANABLE", display=100, by="keyword"):
        """Searches the catalog and returns one dict per physical copy."""
        payload = {
            self.SEARCH_FIELDS[by]: keyword,
            'pubFormCode': 'MO',  # printed books only
            'page': '1',
            'display': str(display),
            'article': order,  # LOANABLE (대출가능순), SCORE (정확도) or RECENT (입고일순)
            'order': 'DESC',
            'manageCode': [self._manage_code(lib_code) if lib_code else "ALL"],
            'kidYn': '',
        }
        try:
            response = self.session.post(f"{self.BASE_URL}/search", json=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            if data.get('result', {}).get('code') != 'Y':
                logger.error(f"Catalog search failed for '{keyword}': {data.get('result')}")
                return None
            return [self._to_holding(book) for book in data['contents']['bookList']]
        except Exception as e:
            logger.error(f"Error searching catalog for '{keyword}': {e}")
            return None

    @staticmethod
    def _to_holding(book):
        return {
            'title': _strip_tags(book['title']),
            'author': _strip_tags(book.get('author', '')),
            'isbn': book.get('isbn', ''),
            'libCode': str(book['libCode']),
            'libName': book.get('libName', ''),
            'loanStatus': book['loanStatus'],          # 대출가능 / 대출불가
            'workingStatus': book.get('workingStatus', ''),  # 비치중 / 대출중 / 예약중 / 상호대차중
            'returnPlanDate': book.get('returnPlanDate', ''),
            'reservationCount': int(book.get('reservationCount') or 0),
            'shelf': book.get('shelfLocName', ''),             # 자료실, e.g. 종합자료실(2층)
            'callNo': book.get('callNo', ''),                  # 청구기호
            'inputDate': book.get('inputDate', ''),            # 입고일
            'mutualLoan': book.get('isActiveMutualLoanYn') == 'Y',  # 상호대차 신청 가능
            'reservable': book.get('isActiveResvYn') == 'Y',        # 예약 가능
        }

    def find_holdings(self, lib_codes, title, isbn=""):
        """Returns the copies of a monitored book held by the given libraries.

        lib_codes: one library code, an iterable of codes, or None for every library.
        The ISBN is tried first. If those libraries have no copy with that ISBN
        (often a different edition picked by an old title search), copies whose
        main title matches are used instead.
        """
        if isinstance(lib_codes, (str, int)):
            lib_codes = [lib_codes]
        wanted = None if lib_codes is None else {str(c) for c in lib_codes}
        # A single library can be filtered by the catalog; otherwise fetch every library
        only_lib = next(iter(wanted)) if wanted and len(wanted) == 1 else None
        display = 100 if only_lib else 300

        def in_scope(h):
            return wanted is None or h['libCode'] in wanted

        if isbn:
            holdings = self.search(isbn, only_lib, display=display)
            if holdings is None:
                return None
            holdings = [h for h in holdings if in_scope(h) and h['isbn'] == isbn]
            if holdings:
                return holdings

        holdings = self.search(title, only_lib, display=display)
        if holdings is None:
            return None
        target = main_title(title)
        return [h for h in holdings if in_scope(h) and main_title(h['title']) == target]


def is_available(holdings):
    return any(h['loanStatus'] == AVAILABLE for h in holdings)


def is_overdue(return_plan_date):
    """True when a 'YYYY.MM.DD' return date is before today (KST)."""
    try:
        due = datetime.strptime(return_plan_date, "%Y.%m.%d").date()
    except (TypeError, ValueError):
        return False
    return due < datetime.now(KST).date()


def describe_holdings(holdings, with_library=False):
    """Summarizes copies as (icon, detail text).

    with_library adds which library the detail is about, for rows that watch
    several libraries.
    """
    if not holdings:
        return "📭", "미소장"
    if is_available(holdings):
        if not with_library:
            return "✅", "대출가능"
        names = list(dict.fromkeys(h['libName'] for h in holdings if is_available([h])))
        more = f" 외 {len(names) - 3}곳" if len(names) > 3 else ""
        return "✅", f"대출가능 ({', '.join(names[:3])}{more})"
    dated = sorted((h for h in holdings if h['returnPlanDate']), key=lambda h: h['returnPlanDate'])
    first = dated[0] if dated else holdings[0]
    detail = first['workingStatus'] or "대출불가"
    if first['returnPlanDate']:
        if is_overdue(first['returnPlanDate']):
            detail += f" · ⚠️ 연체 중 (반납예정 {first['returnPlanDate']})"
        else:
            detail += f" · 반납예정 {first['returnPlanDate']}"
    reservations = sum(h['reservationCount'] for h in holdings)
    if reservations:
        detail += f" · 예약 {reservations}명"
    if with_library:
        detail += f" ({first['libName']})"
    return "❌", detail


def main_title(title):
    """Normalizes a title for comparison: drops a leading (series)/[shelf] note,
    the subtitle after ':' or '=', and all spaces and punctuation."""
    title = _strip_tags(title)
    title = re.sub(r'^\s*[\(\[][^\)\]]*[\)\]]\s*', '', title)
    title = re.split(r'[:=]', title)[0]
    return _compact(title)


def match_tier(query, title, author=""):
    """Ranks how well a book matches a search query; lower is better, None is no match.

    0 main title equals the query, 1 main title starts with it, 2 the whole title
    (subtitle included) contains it, 3 the main title or the author contains every
    word of it. Words found only in a subtitle (e.g. '학습' in '서바이벌 학습만화')
    don't count, since that is what made keyword search return unrelated books.
    """
    q = _compact(query)
    if not q:
        return None
    main = main_title(title)
    if main == q:
        return 0
    if main.startswith(q):
        return 1
    if q in _compact(title):
        return 2
    words = [w for w in (_compact(w) for w in query.split()) if w]
    if all(w in main for w in words) or all(w in _compact(author) for w in words):
        return 3
    return None


def _compact(text):
    return re.sub(r'[^0-9A-Za-z가-힣]', '', _strip_tags(text)).lower()


def _strip_tags(text):
    # Keyword searches wrap matches in <span class='highlight word'>
    return html.unescape(re.sub(r'<[^>]+>', '', text or '')).strip()
