# ============================================================
# ZERODHA DAILY LOGIN HANDLER
# Handles the mandatory daily authentication for Zerodha API
# Flow:
# 1. Bot sends login URL to Telegram at 8:00 AM
# 2. You click link, login with PIN
# 3. Zerodha redirects to local URL with request_token
# 4. This script catches token and exchanges for access_token
# 5. Access token saved to file for bot to use
# ============================================================

import os
import json
import logging
import requests
import hashlib
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading
import time

logger = logging.getLogger(__name__)

TOKEN_FILE = 'logs/zerodha_token.json'
LOGIN_TIMEOUT = 300  # 5 minutes to complete login


class TokenCatcher(BaseHTTPRequestHandler):
    """
    Simple HTTP server that catches the Zerodha redirect.
    Zerodha redirects to http://127.0.0.1/?request_token=XXXX
    This server catches that request and extracts the token.
    """
    captured_token = None
    captured_status = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if 'request_token' in params:
            TokenCatcher.captured_token = params['request_token'][0]
            TokenCatcher.captured_status = 'success'
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b'''
                <html><body style="font-family:Arial;text-align:center;
                margin-top:100px;background:#1a1a2e;color:#00ff88">
                <h1>Login Successful!</h1>
                <p>TradingBot authenticated. You can close this window.</p>
                <p>Trading starts at 9:15 AM IST</p>
                </body></html>
            ''')
            logger.info("Request token captured successfully")
        elif 'error' in params:
            TokenCatcher.captured_status = 'error'
            self.send_response(400)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<html><body>Login failed. Please try again.</body></html>')
            logger.error("Login error: %s", params.get('error'))
        else:
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<html><body>Waiting for login...</body></html>')

    def log_message(self, format, *args):
        pass  # Suppress HTTP server logs


class ZerodhaLogin:

    def __init__(self, api_key: str, api_secret: str, telegram_config: dict):
        self.api_key = api_key
        self.api_secret = api_secret
        self.telegram_token = telegram_config.get('bot_token', '')
        self.telegram_chat_id = telegram_config.get('chat_id', '')
        self.access_token = None
        self.kite = None

    def get_login_url(self) -> str:
        """Generate the Zerodha login URL."""
        return (f"https://kite.zerodha.com/connect/login"
                f"?api_key={self.api_key}&v=3")

    def send_login_link(self):
        """Send login link to Telegram."""
        login_url = self.get_login_url()
        msg = (
 	   "Good Morning! Daily Login Required\n\n"
   	   "Step 1: Click this link:\n"
   	   + login_url + "\n\n"
   	   "Step 2: Enter Zerodha password and PIN\n\n"
   	   "Step 3: Browser will show error page\n"
    	   "Copy the FULL URL from address bar\n\n"
           "Step 4: PASTE that URL here in this chat\n\n"
   	   "Bot will handle the rest automatically!"
	)
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            requests.post(url, json={
                'chat_id': self.telegram_chat_id,
                'text': msg,
                'disable_notification': False,
            }, timeout=10)
            # Send a second alert 30 seconds later as reminder
            import threading
            def reminder():
                import time
                time.sleep(30)
                requests.post(url, json={
                    'chat_id': self.telegram_chat_id,
                    'text': '⚠️ LOGIN PENDING — Paste the redirect URL here to start trading!',
                    'disable_notification': False,
                }, timeout=10)
            threading.Thread(target=reminder, daemon=True).start()
            logger.info("Login link sent to Telegram")
        except Exception as e:
            logger.error("Failed to send login link: %s", e)

    def wait_for_login(self, timeout: int = LOGIN_TIMEOUT) -> bool:
        """
        Start local HTTP server and wait for Zerodha redirect.
        Returns True if login successful, False if timeout.
        """
        TokenCatcher.captured_token = None
        TokenCatcher.captured_status = None

        # Start HTTP server on port 80
        try:
            server = HTTPServer(('0.0.0.0', 80), TokenCatcher)
        except PermissionError:
            # Port 80 needs root. Try 8080
            try:
                server = HTTPServer(('0.0.0.0', 8080), TokenCatcher)
                logger.info("Using port 8080 for token capture")
            except Exception as e:
                logger.error("Cannot start token server: %s", e)
                return False

        server.timeout = 1  # Check every second

        logger.info("Waiting for Zerodha login (timeout: %ds)...", timeout)
        start_time = time.time()

        while time.time() - start_time < timeout:
            server.handle_request()
            if TokenCatcher.captured_token:
                server.server_close()
                return self._exchange_token(TokenCatcher.captured_token)
            if TokenCatcher.captured_status == 'error':
                server.server_close()
                return False

        server.server_close()
        logger.error("Login timeout after %d seconds", timeout)
        return False

    def _exchange_token(self, request_token: str) -> bool:
        """Exchange request token for access token."""
        try:
            # Generate checksum
            checksum_str = self.api_key + request_token + self.api_secret
            checksum = hashlib.sha256(checksum_str.encode()).hexdigest()

            # Exchange tokens
            url = "https://api.kite.trade/session/token"
            resp = requests.post(url, data={
                'api_key': self.api_key,
                'request_token': request_token,
                'checksum': checksum,
            }, timeout=15)

            if resp.status_code == 200:
                data = resp.json()
                self.access_token = data['data']['access_token']
                user_id = data['data']['user_id']
                self._save_token(self.access_token, user_id)
                logger.info("Access token obtained for user: %s", user_id)
                self._send_success_alert(user_id)
                return True
            else:
                logger.error("Token exchange failed: %s %s",
                             resp.status_code, resp.text)
                return False

        except Exception as e:
            logger.error("Token exchange error: %s", e)
            return False

    def _save_token(self, access_token: str, user_id: str):
        """Save access token to file for bot to use."""
        os.makedirs('logs', exist_ok=True)
        data = {
            'access_token': access_token,
            'user_id': user_id,
            'date': str(date.today()),
            'time': datetime.now().strftime('%H:%M:%S'),
            'api_key': self.api_key,
        }
        with open(TOKEN_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        logger.info("Token saved to %s", TOKEN_FILE)

    def _send_success_alert(self, user_id: str):
        """Send success notification to Telegram."""
        try:
            msg = (
                "Zerodha Login Successful!\n"
                "User: " + user_id + "\n"
                "Time: " + datetime.now().strftime('%H:%M:%S') + "\n"
                "Bot will start trading at 9:15 AM\n"
                "Capital: Rs.50,000 ready"
            )
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            requests.post(url, json={
                'chat_id': self.telegram_chat_id,
                'text': msg,
                'disable_notification': False,
            }, timeout=10)
            # Send a second alert 30 seconds later as reminder
            import threading
            def reminder():
                import time
                time.sleep(30)
                requests.post(url, json={
                    'chat_id': self.telegram_chat_id,
                    'text': '⚠️ LOGIN PENDING — Paste the redirect URL here to start trading!',
                    'disable_notification': False,
                }, timeout=10)
            threading.Thread(target=reminder, daemon=True).start()
        except Exception as e:
            logger.debug("Success alert error: %s", e)

    @staticmethod
    def load_saved_token() -> dict:
        """Load today's saved token. Returns None if no valid token."""
        try:
            if not os.path.exists(TOKEN_FILE):
                return None
            with open(TOKEN_FILE, 'r') as f:
                data = json.load(f)
            # Check if token is from today
            if data.get('date') == str(date.today()):
                logger.info("Valid token found for today")
                return data
            else:
                logger.info("Token is from %s - need fresh login",
                            data.get('date'))
                return None
        except Exception as e:
            logger.error("Token load error: %s", e)
            return None

    @staticmethod
    def is_token_valid() -> bool:
        """Check if we have a valid token for today."""
        token_data = ZerodhaLogin.load_saved_token()
        return token_data is not None

    def get_kite_instance(self):
        """Get authenticated KiteConnect instance."""
        try:
            from kiteconnect import KiteConnect
            token_data = self.load_saved_token()
            if not token_data:
                return None
            kite = KiteConnect(api_key=self.api_key)
            kite.set_access_token(token_data['access_token'])
            self.kite = kite
            return kite
        except Exception as e:
            logger.error("Kite instance error: %s", e)
            return None


def do_daily_login(api_key: str, api_secret: str,
                   telegram_config: dict) -> bool:
    """
    Complete daily login flow.
    Returns True if login successful.
    """
    login = ZerodhaLogin(api_key, api_secret, telegram_config)

    # Check if already logged in today
    if ZerodhaLogin.is_token_valid():
        logger.info("Already logged in today - skipping login")
        return True

    # Send login link to Telegram
    login.send_login_link()

    # Wait for login
    success = login.wait_for_login(timeout=LOGIN_TIMEOUT)

    if success:
        logger.info("Daily login completed successfully")
    else:
        logger.error("Daily login failed - bot will not trade today")

    return success
