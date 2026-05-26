import hashlib, json, os, sys, time, logging, requests
from datetime import date, datetime

sys.path.insert(0, '/home/jvrakhilesh97/tradingbot')

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/home/jvrakhilesh97/tradingbot/logs/telegram_login.log', mode='a')
    ])
logger = logging.getLogger(__name__)

TOKEN_FILE = '/home/jvrakhilesh97/tradingbot/logs/zerodha_token.json'

def get_settings():
    import importlib
    import config.settings as s
    importlib.reload(s)
    return s.BROKER, s.TELEGRAM

def send_msg(msg):
    _, telegram = get_settings()
    try:
        requests.post(
            f'https://api.telegram.org/bot{telegram["bot_token"]}/sendMessage',
            json={'chat_id': str(telegram['chat_id']), 'text': msg},
            timeout=10)
    except Exception as e:
        logger.error("Send error: %s", e)

def exchange_token(request_token):
    broker, _ = get_settings()
    api_key = broker['api_key']
    api_secret = broker['api_secret']
    logger.info("Exchanging token with api_key: %s...", api_key[:4])
    checksum = hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()
    resp = requests.post('https://api.kite.trade/session/token',
        data={'api_key': api_key, 'request_token': request_token, 'checksum': checksum},
        timeout=15)
    if resp.status_code == 200:
        data = resp.json()['data']
        token_data = {
            'access_token': data['access_token'],
            'user_id': data['user_id'],
            'date': str(date.today()),
            'time': datetime.now().strftime('%H:%M:%S'),
            'api_key': api_key
        }
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, 'w') as f:
            json.dump(token_data, f, indent=2)
        logger.info("Token saved for user: %s", data['user_id'])
        return True, data['user_id']
    else:
        logger.error("Token exchange failed: %s", resp.text)
        return False, resp.text

def is_valid_today():
    try:
        with open(TOKEN_FILE) as f:
            d = json.load(f)
        return d.get('date') == str(date.today())
    except:
        return False

def extract_token(text):
    text = text.strip()
    if 'request_token=' in text:
        return text.split('request_token=')[1].split('&')[0].strip()
    if len(text) > 20 and ' ' not in text and '/' not in text and 'http' not in text:
        return text
    return None

def run():
    logger.info("Telegram login bot started")
    send_msg("Telegram login bot ready!\nPaste the redirect URL here after Zerodha login.")

    _, telegram = get_settings()
    bot_token = telegram['bot_token']
    chat_id = str(telegram['chat_id'])

    last_id = 0
    try:
        resp = requests.get(f'https://api.telegram.org/bot{bot_token}/getUpdates',
            params={'limit': 1}, timeout=10)
        updates = resp.json().get('result', [])
        if updates:
            last_id = updates[-1]['update_id']
    except:
        pass

    while True:
        try:
            broker, telegram = get_settings()
            bot_token = telegram['bot_token']
            chat_id = str(telegram['chat_id'])

            resp = requests.get(
                f'https://api.telegram.org/bot{bot_token}/getUpdates',
                params={'offset': last_id + 1, 'timeout': 30,
                        'allowed_updates': ['message']},
                timeout=35)

            if resp.status_code != 200:
                time.sleep(5)
                continue

            for update in resp.json().get('result', []):
                last_id = update['update_id']
                msg = update.get('message', {})
                text = msg.get('text', '').strip()
                msg_chat_id = str(msg.get('chat', {}).get('id', ''))

                if msg_chat_id != chat_id or not text:
                    continue

                logger.info("Message received: %s", text[:50])

                if text.lower() == 'status':
                    valid = is_valid_today()
                    active = os.system("sudo systemctl is-active --quiet tradingbot") == 0
                    send_msg("Status:\nLogin: " + ("Yes" if valid else "No") +
                             "\nBot: " + ("Running" if active else "Stopped") +
                             "\nTime: " + datetime.now().strftime('%H:%M IST'))
                    continue

                if text.lower() in ['resend', 'login', 'link']:
                    if is_valid_today():
                        send_msg("Already logged in today! Bot is running.")
                        continue
                    broker, _ = get_settings()
                    api_key = broker['api_key']
                    login_url = 'https://kite.zerodha.com/connect/login?api_key=' + api_key + '&v=3'
                    send_msg(
                        "FRESH LOGIN LINK\n\n"
                        "Step 1: Click:\n" + login_url + "\n\n"
                        "Step 2: Enter password + PIN\n\n"
                        "Step 3: Copy full URL\n\n"
                        "Step 4: Paste URL here"
                    )
                    logger.info("Login link resent on request")
                    continue


                token = extract_token(text)
                if not token:
                    continue

                if is_valid_today():
                    send_msg("Already logged in today! Bot is running.")
                    continue

                send_msg("Processing login...")
                ok, result = exchange_token(token)

                if ok:
                    send_msg("Login Successful!\nUser: " + str(result) + "\nRestarting bot...")
                    os.system("sudo systemctl restart tradingbot")
                    send_msg("Bot started! Trading begins at 9:15 AM")
                else:
                    send_msg("Login FAILED: " + str(result) + "\nPlease try again with a fresh URL.")

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            logger.error("Error: %s", e)
            time.sleep(10)

if __name__ == '__main__':
    run()
