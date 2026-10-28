import html
import logging
import re
import requests

logger = logging.getLogger(__name__)

AVAILABLE = "대출가능"
SEARCH_PAGE_URL = "https://alpasq.bcl.go.kr/search/keyword/"


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

    def search(self, keyword, lib_code=None, order="LOANABLE", display=100):
        """Searches the catalog and returns one dict per physical copy."""
        payload = {
            'searchKeyword': keyword,
            'pubFormCode': 'MO',  # printed books only
            'page': '1',
            'display': str(display),
            'article': order,  # LOANABLE (대출가능순) or SCORE (정확도)
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
        }

    def find_holdings(self, lib_code, title, isbn=""):
        """Returns the copies of a monitored book held by one library.

        The ISBN is tried first. If that library has no copy with that ISBN
        (often a different edition picked by an old title search), copies whose
        main title matches are used instead.
        """
        lib_code = str(lib_code)
        if isbn:
            holdings = self.search(isbn, lib_code)
            if holdings is None:
                return None
            holdings = [h for h in holdings if h['libCode'] == lib_code and h['isbn'] == isbn]
            if holdings:
                return holdings

        holdings = self.search(title, lib_code)
        if holdings is None:
            return None
        wanted = main_title(title)
        return [h for h in holdings if h['libCode'] == lib_code and main_title(h['title']) == wanted]


def is_available(holdings):
    return any(h['loanStatus'] == AVAILABLE for h in holdings)


def describe_holdings(holdings):
    """Summarizes the copies at one library as (icon, detail text)."""
    if not holdings:
        return "📭", "미소장"
    if is_available(holdings):
        return "✅", "대출가능"
    dated = sorted((h for h in holdings if h['returnPlanDate']), key=lambda h: h['returnPlanDate'])
    first = dated[0] if dated else holdings[0]
    detail = first['workingStatus'] or "대출불가"
    if first['returnPlanDate']:
        detail += f" · 반납예정 {first['returnPlanDate']}"
    reservations = sum(h['reservationCount'] for h in holdings)
    if reservations:
        detail += f" · 예약 {reservations}명"
    return "❌", detail


def main_title(title):
    """Normalizes a title for comparison: drops a leading (series)/[shelf] note,
    the subtitle after ':' or '=', and all spaces and punctuation."""
    title = _strip_tags(title)
    title = re.sub(r'^\s*[\(\[][^\)\]]*[\)\]]\s*', '', title)
    title = re.split(r'[:=]', title)[0]
    return re.sub(r'[^0-9A-Za-z가-힣]', '', title).lower()


def _strip_tags(text):
    # Keyword searches wrap matches in <span class='highlight word'>
    return html.unescape(re.sub(r'<[^>]+>', '', text or '')).strip()
