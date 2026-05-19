# ============================================================
# GITHUB AUTO-SYNC
# Pushes trades.json and skipped_signals.json to GitHub
# after every trade close or skip event
# Called automatically by main.py
# ============================================================

import os
import json
import base64
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

GITHUB_USER = "JakkaAkhilesh"
GITHUB_REPO = "tradelog"
GITHUB_BRANCH = "main"
GITHUB_API = "https://api.github.com"

# Files to sync
FILES_TO_SYNC = [
    {
        'local': 'logs/trades.json',
        'remote': 'trades.json',
        'description': 'Trade history'
    },
    {
        'local': 'logs/skipped_signals.json',
        'remote': 'skipped_signals.json',
        'description': 'Skipped signals'
    }
]


def get_github_token() -> str:
    """Read GitHub token from environment or config."""
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        try:
            import sys
            sys.path.insert(0, '.')
            from config.settings import GITHUB
            token = GITHUB.get('token', '')
        except Exception:
            pass
    return token


def get_file_sha(token: str, remote_path: str) -> str:
    """Get SHA of existing file on GitHub (needed for updates)."""
    url = f"{GITHUB_API}/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{remote_path}"
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json'
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get('sha', '')
    except Exception as e:
        logger.debug("SHA fetch error: %s", e)
    return ''


def push_file(token: str, local_path: str, remote_path: str,
              commit_msg: str) -> bool:
    """Push a single file to GitHub."""
    try:
        if not os.path.exists(local_path):
            logger.warning("File not found: %s", local_path)
            return False

        with open(local_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Encode content
        encoded = base64.b64encode(content.encode('utf-8')).decode('utf-8')

        # Get existing SHA if file exists
        sha = get_file_sha(token, remote_path)

        url = (f"{GITHUB_API}/repos/{GITHUB_USER}/{GITHUB_REPO}"
               f"/contents/{remote_path}")
        headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        payload = {
            'message': commit_msg,
            'content': encoded,
            'branch': GITHUB_BRANCH
        }
        if sha:
            payload['sha'] = sha

        resp = requests.put(url, headers=headers,
                           json=payload, timeout=15)

        if resp.status_code in (200, 201):
            logger.info("Synced %s to GitHub", remote_path)
            return True
        else:
            logger.error("GitHub push failed: %d %s",
                        resp.status_code, resp.text[:200])
            return False

    except Exception as e:
        logger.error("GitHub sync error for %s: %s", remote_path, e)
        return False


def sync_all(reason: str = 'trade_update') -> bool:
    """Sync all trade files to GitHub."""
    token = get_github_token()
    if not token:
        logger.warning("No GitHub token — skipping sync")
        return False

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    commit_msg = f"bot: {reason} [{timestamp} IST]"

    success = True
    for file_info in FILES_TO_SYNC:
        ok = push_file(
            token,
            file_info['local'],
            file_info['remote'],
            commit_msg
        )
        if not ok:
            success = False

    return success


def sync_after_trade(trade: dict):
    """Called after every trade close."""
    strategy = trade.get('strategy', 'unknown')
    result = trade.get('result', 'unknown')
    pnl = trade.get('pnl', 0)
    reason = f"{strategy} {result} Rs.{pnl}"
    sync_all(reason=reason)


def sync_after_skip(signal: dict):
    """Called after every skipped signal."""
    strategy = signal.get('strategy', 'unknown')
    skip_reason = signal.get('skip_reason', 'unknown')
    reason = f"skip {strategy} {skip_reason}"
    sync_all(reason=reason)


if __name__ == '__main__':
    # Test sync
    logging.basicConfig(level=logging.INFO)
    logger.info("Testing GitHub sync...")
    result = sync_all(reason='manual_test')
    print("Sync result:", "SUCCESS" if result else "FAILED")
