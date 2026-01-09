import os
import json
import logging
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import time
from dotenv import load_dotenv

# Load environment variables from .env file (for local development)
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ConfigManager:
    """Manages configuration and secrets."""
    def __init__(self):
        self.lib_api_key = os.getenv("LIBRARY_API_KEY")
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.google_creds_json = os.getenv("GOOGLE_SHEET_CREDENTIALS")
        self.google_sheet_url = os.getenv("GOOGLE_SHEET_URL") # Or name

        if not all([self.lib_api_key, self.telegram_bot_token, self.telegram_chat_id, self.google_creds_json, self.google_sheet_url]):
            logger.warning("One or more environment variables are missing. Some features may not work.")

    def get_google_creds(self):
        try:
            json_str = self.google_creds_json
            if not json_str:
                logger.error("GOOGLE_SHEET_CREDENTIALS is empty")
                return None

            logger.info(f"JSON length: {len(json_str)}")

            # Try parsing as-is first (for local .env)
            try:
                creds_dict = json.loads(json_str)
                logger.info("JSON parsed successfully (first try)")
            except json.JSONDecodeError as e:
                logger.info(f"First JSON parse failed: {e}, trying fix...")
                # Fix for GitHub Secrets converting \n to actual newlines in private_key
                import re
                def fix_newlines(match):
                    return match.group(0).replace('\n', '\\n')
                json_str = re.sub(r'"private_key"\s*:\s*"[^"]*"', fix_newlines, json_str, flags=re.DOTALL)
                creds_dict = json.loads(json_str)
                logger.info("JSON parsed successfully (after fix)")

            logger.info(f"Keys in creds_dict: {list(creds_dict.keys())}")

            # Normalize private_key format for RSA
            if 'private_key' in creds_dict:
                pk = creds_dict['private_key']
                logger.info(f"Original private_key length: {len(pk)}")
                # Remove carriage returns
                pk = pk.replace('\r', '')
                # Convert literal \\n to actual newlines
                pk = pk.replace('\\n', '\n')
                # Fix broken BEGIN/END markers (GitHub Secrets sometimes breaks these)
                pk = pk.replace('-----BEGIN PRIVATE\n  KEY-----', '-----BEGIN PRIVATE KEY-----')
                pk = pk.replace('-----END PRIVATE\n  KEY-----', '-----END PRIVATE KEY-----')
                pk = pk.replace('-----BEGIN PRIVATE \nKEY-----', '-----BEGIN PRIVATE KEY-----')
                pk = pk.replace('-----END PRIVATE \nKEY-----', '-----END PRIVATE KEY-----')
                # More generic fix - remove any whitespace issues in markers
                import re
                pk = re.sub(r'-----BEGIN\s+PRIVATE\s+KEY-----', '-----BEGIN PRIVATE KEY-----', pk)
                pk = re.sub(r'-----END\s+PRIVATE\s+KEY-----', '-----END PRIVATE KEY-----', pk)
                # Ensure single newlines (no double newlines)
                while '\n\n' in pk:
                    pk = pk.replace('\n\n', '\n')
                creds_dict['private_key'] = pk
                logger.info(f"Private key starts with: {pk[:60]}")
                logger.info(f"Private key ends with: {pk[-40:]}")

            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            logger.info("About to create credentials...")
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            logger.info("Credentials created successfully!")
            return creds
        except Exception as e:
            logger.error(f"Failed to parse Google Credentials: {e}")
            logger.error(f"JSON starts with: {self.google_creds_json[:100] if self.google_creds_json else 'EMPTY'}")
            return None

class LibraryClient:
    """Interacts with the Library Information Naru API."""
    BASE_URL = "http://data4library.kr/api"

    def __init__(self, api_key):
        self.api_key = api_key

    def search_book_isbn(self, title):
        """Searches for a book by title and returns the ISBN13 of the first result."""
        # Try with original title first, then without spaces
        for search_title in [title, title.replace(' ', '')]:
            params = {
                'authKey': self.api_key,
                'title': search_title,
                'format': 'json',
                'pageSize': 1
            }
            try:
                response = requests.get(f"{self.BASE_URL}/srchBooks", params=params)
                response.raise_for_status()
                data = response.json()
                if data.get('response', {}).get('docs'):
                    return data['response']['docs'][0]['doc']['isbn13']
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
            response = requests.get(f"{self.BASE_URL}/bookExist", params=params)
            response.raise_for_status()
            data = response.json()
            if 'response' in data and 'result' in data['response']:
                # The API documentation says 'loanAvailable' 'Y' or 'N'
                return data['response']['result']['loanAvailable']
            return None
        except Exception as e:
            logger.error(f"Error checking availability for ISBN {isbn13} at Lib {lib_code}: {e}")
            return None

class Notifier:
    """Sends notifications via Telegram."""
    def __init__(self, bot_token, chat_id):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send_message(self, message):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            'chat_id': self.chat_id,
            'text': message
        }
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            logger.info("Telegram notification sent.")
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

class StateManager:
    """Manages the state of book availability."""
    FILE_PATH = "status.json"

    @staticmethod
    def load_state():
        if os.path.exists(StateManager.FILE_PATH):
            try:
                with open(StateManager.FILE_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load state: {e}")
        return {}

    @staticmethod
    def save_state(state):
        try:
            with open(StateManager.FILE_PATH, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

def main():
    logger.info("Starting Bucheon Library Monitor...")
    config = ConfigManager()
    
    # 1. Google Sheets Connection
    creds = config.get_google_creds()
    if not creds:
        logger.error("Could not authenticate with Google Sheets.")
        return

    try:
        client = gspread.authorize(creds)
        sheet = client.open_by_url(config.google_sheet_url).sheet1
        rows = sheet.get_all_records() # Expecting headers like 'Title', 'LibraryCode', 'LibraryName'
    except Exception as e:
        logger.error(f"Error reading Google Sheet: {e}")
        return

    lib_client = LibraryClient(config.lib_api_key)
    notifier = Notifier(config.telegram_bot_token, config.telegram_chat_id)
    state = StateManager.load_state()
    new_state = state.copy()
    
    changes_detected = False

    for row in rows:
        title = row.get('Title')
        lib_code = row.get('LibraryCode') # Should be string
        lib_name = row.get('LibraryName', 'Unknown Library')
        
        if not title or not lib_code:
            continue
            
        logger.info(f"Processing: {title} @ {lib_name} ({lib_code})")

        # Resolve ISBN (optimally we could cache this in the sheet or state to avoid searching every time)
        # For now, we search every time or rely on provided ISBN if added to sheet
        isbn = row.get('ISBN')
        if not isbn:
            isbn = lib_client.search_book_isbn(title)
            if not isbn:
                logger.warning(f"Could not find ISBN for '{title}'")
                continue
        
        # Check Availability
        availability = lib_client.check_availability(lib_code, isbn)
        if not availability:
            logger.warning(f"Could not check availability for '{title}'")
            continue

        # State Key: ISBN + LibraryCode
        key = f"{isbn}_{lib_code}"
        last_status = state.get(key, 'N') # Default to N if not seen before
        
        logger.info(f"Status: {availability} (Last: {last_status})")

        if last_status == 'N' and availability == 'Y':
            import urllib.parse
            encoded_title = urllib.parse.quote(title)
            # Bucheon Library Search URL (Example, adjust as needed)
            search_url = f"https://library.bucheon.go.kr/library/search/page1.do?title={encoded_title}"
            
            message = f"📚 **Available Now!**\n\n📖 **{title}**\n📍 {lib_name}\n\n[Reserve/Check Details]({search_url})"
            notifier.send_message(message)
            changes_detected = True
        
        new_state[key] = availability
        
        # Be nice to the API
        time.sleep(0.5)

    if changes_detected or new_state != state:
        StateManager.save_state(new_state)
        logger.info("State updated.")
    else:
        logger.info("No changes in state.")

if __name__ == "__main__":
    main()
