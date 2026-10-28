import hashlib
import json
import logging
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

logger = logging.getLogger(__name__)

# Watch-list column holding the last seen availability (Y/N). State lives in the
# sheet so it survives restarts and moving the bot to another machine.
STATUS_HEADER = "Status"

# Extra tabs, created on first use
SETTINGS_SHEET = "Settings"            # Key | Value (e.g. home_libraries)
# Query | Seen | ISBN. Keyword subscriptions keep the ISBNs already announced in
# Seen; ISBN subscriptions (one exact book) keep the libraries already holding it.
SUBSCRIPTIONS_SHEET = "Subscriptions"
SUBSCRIPTION_HEADERS = ["Query", "Seen", "ISBN"]


def short_key(*parts):
    """Short stable id for a sheet row, small enough for Telegram button data (64 bytes)."""
    return hashlib.sha1("|".join(str(p).strip() for p in parts).encode()).hexdigest()[:10]


def book_key(row):
    return short_key(row.get('Title', ''), row.get('LibraryCode', ''))


class SheetManager:
    """Google Sheet storage: the watch list (first tab), settings and new-arrival subscriptions."""

    def __init__(self, credentials_json, sheet_url):
        self.spreadsheet = None
        self.sheet = None
        self._settings = None
        self._subscription_headers_checked = False
        self._connect(credentials_json, sheet_url)

    def _connect(self, credentials_json, sheet_url):
        try:
            json_str = credentials_json
            if not json_str:
                logger.error("GOOGLE_SHEET_CREDENTIALS is empty")
                return

            # Robust JSON parsing (handles potential newline issues from environment variables/secrets)
            try:
                creds_dict = json.loads(json_str)
            except json.JSONDecodeError:
                # Fix for common formatting issues in private_key
                def fix_newlines(match):
                    return match.group(0).replace('\n', '\\n')
                json_str = re.sub(r'"private_key"\s*:\s*"[^"]*"', fix_newlines, json_str, flags=re.DOTALL)
                creds_dict = json.loads(json_str)

            # Normalize private_key format
            if 'private_key' in creds_dict:
                creds_dict['private_key'] = creds_dict['private_key'].replace('\\n', '\n')

            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            client = gspread.authorize(creds)
            self.spreadsheet = client.open_by_url(sheet_url)
            self.sheet = self.spreadsheet.sheet1
            logger.info("Successfully connected to Google Sheets")
        except Exception as e:
            logger.error(f"Failed to connect to Google Sheet: {e}")

    # --- Watch list -------------------------------------------------------

    def get_all_books(self):
        """Returns all books from the sheet."""
        if not self.sheet:
            return []
        try:
            return self.sheet.get_all_records()
        except Exception as e:
            logger.error(f"Error reading sheet: {e}")
            return []

    def add_book(self, title, lib_code, lib_name, isbn=""):
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
        return self._delete_first(lambda row: str(row.get('Title', '')).strip().lower() == title.strip().lower())

    def delete_by_key(self, key):
        """Deletes the row whose book_key matches; returns its title, or None."""
        return self._delete_first(lambda row: book_key(row) == key)

    def _delete_first(self, matches):
        if not self.sheet:
            return None
        try:
            for i, row in enumerate(self.sheet.get_all_records(), start=2):  # Row 1 is the header
                if matches(row):
                    self.sheet.delete_rows(i)
                    return str(row.get('Title', '')) or True
            return None
        except Exception as e:
            logger.error(f"Error deleting book: {e}")
            return None

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

    # --- Extra tabs ---------------------------------------------------------

    def _worksheet(self, name, headers):
        """Returns a tab by name, creating it with a header row if missing."""
        try:
            return self.spreadsheet.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = self.spreadsheet.add_worksheet(title=name, rows=100, cols=len(headers))
            ws.append_row(headers)
            return ws

    def _replace_row(self, ws, key, values):
        """Replaces the row whose first cell is key (or appends one). Values are written
        RAW, so codes like '141652,141660' aren't turned into numbers."""
        keys = ws.col_values(1)
        if key in keys[1:]:
            ws.delete_rows(keys.index(key) + 1)
        ws.append_row(values)

    def reload_settings(self):
        """Re-reads the Settings tab (picks up edits made directly in the sheet)."""
        self._settings = None

    def get_setting(self, key, default=""):
        if self._settings is None:
            if not self.spreadsheet:
                return default
            try:
                rows = self._worksheet(SETTINGS_SHEET, ["Key", "Value"]).get_all_records()
                self._settings = {str(r['Key']): str(r['Value']) for r in rows}
            except Exception as e:
                logger.error(f"Error reading settings: {e}")
                return default
        return self._settings.get(key, default)

    def set_setting(self, key, value):
        try:
            self._replace_row(self._worksheet(SETTINGS_SHEET, ["Key", "Value"]), key, [key, value])
            if self._settings is not None:
                self._settings[key] = value
            return True
        except Exception as e:
            logger.error(f"Error saving setting {key}: {e}")
            return False

    def _subscriptions_sheet(self):
        ws = self._worksheet(SUBSCRIPTIONS_SHEET, SUBSCRIPTION_HEADERS)
        if not getattr(self, '_subscription_headers_checked', False):
            # Tabs created before ISBN subscriptions existed lack the ISBN column
            headers = ws.row_values(1)
            for name in SUBSCRIPTION_HEADERS:
                if name not in headers:
                    headers.append(name)
                    if len(headers) > ws.col_count:
                        ws.add_cols(len(headers) - ws.col_count)
                    ws.update_cell(1, len(headers), name)
            self._subscription_headers_checked = True
        return ws

    def get_subscriptions(self):
        """Returns [{'Query': str, 'Seen': set, 'ISBN': str}] for new-arrival alerts."""
        if not self.spreadsheet:
            return []
        try:
            rows = self._subscriptions_sheet().get_all_records()
        except Exception as e:
            logger.error(f"Error reading subscriptions: {e}")
            return []
        return [{'Query': str(r['Query']), 'Seen': {s for s in str(r['Seen']).split(',') if s},
                 'ISBN': str(r.get('ISBN', '')).strip()}
                for r in rows if str(r.get('Query', '')).strip()]

    def save_subscription(self, query, seen, isbn=""):
        """Adds a subscription or replaces its seen set."""
        try:
            self._replace_row(self._subscriptions_sheet(), query, [query, ",".join(sorted(seen)), isbn])
            return True
        except Exception as e:
            logger.error(f"Error saving subscription '{query}': {e}")
            return False

    def delete_subscription(self, key):
        """Deletes the subscription whose short_key(query) matches; returns its query, or None."""
        try:
            ws = self._subscriptions_sheet()
            for i, query in enumerate(ws.col_values(1)[1:], start=2):
                if short_key(query) == key:
                    ws.delete_rows(i)
                    return query
        except Exception as e:
            logger.error(f"Error deleting subscription: {e}")
        return None
