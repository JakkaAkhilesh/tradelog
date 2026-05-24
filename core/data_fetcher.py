# ============================================================
# DATA FETCHER — UPDATED VERSION
# New features:
# 1. NSE option chain fetch (real live premium)
# 2. India VIX fetch with retry + cache + fallback
# 3. Weekend + holiday aware is_market_open()
# ============================================================

import time
import json
import os
import logging
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, date, timedelta

logger = logging.getLogger(__name__)

NSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer': 'https://www.nseindia.com/option-chain',
    'Connection': 'keep-alive',
    'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
    'X-Requested-With': 'XMLHttpRequest',
}

VIX_CACHE_FILE = 'logs/vix_history.json'
PREMIUM_CACHE = {}   # In-memory: {symbol: [p1, p2, ...last 10 prices]}


class DataFetcher:

    def __init__(self, settings: dict):
        self.settings = settings
        self.index_symbol = settings.get('INDEX', {}).get('yf_symbol', '^NSEI')
        self._nse_session = None
        self._nse_session_time = None
        self._last_vix = None
        self._vix_10d_avg = None
        os.makedirs('logs', exist_ok=True)
        self._load_vix_history()

    # ── NSE SESSION ─────────────────────────────────────────
    def _get_nse_session(self):
        now = datetime.now()
        if (self._nse_session and self._nse_session_time and
                (now - self._nse_session_time).seconds < 300):
            return self._nse_session
        try:
            session = requests.Session()
            session.headers.update(NSE_HEADERS)
            # Hit main page first to get cookies
            session.get('https://www.nseindia.com', timeout=10)
            time.sleep(1)  # ADD THIS LINE — wait 1 second for cookies
            # Then hit option chain page to warm up session
            session.get('https://www.nseindia.com/market-data/live-equity-market',
                        timeout=10)
            time.sleep(0.5)  # ADD THIS LINE
            self._nse_session = session
            self._nse_session_time = now
            return session
        except Exception as e:
            logger.warning("NSE session failed: %s", e)
            return None

    # ── REAL OPTION PREMIUM ──────────────────────────────────
    def get_option_premium(self, strike: int, option_type: str,
                           expiry_str: str = None) -> float:
        """
        Fetch real live option premium from NSE option chain.
        Returns LTP (last traded price) of the option.
        Falls back to ATR estimate if NSE unavailable.

        option_type: 'CE' or 'PE'
        strike: e.g. 24250
        expiry_str: e.g. '08-May-2026' (nearest expiry if None)
        """
        try:
            session = self._get_nse_session()
            if not session:
                return self._estimate_premium_fallback()

            url = 'https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY'
            resp = session.get(url, timeout=10)

            if resp.status_code != 200:
                logger.warning("NSE option chain: status %d", resp.status_code)
                return self._estimate_premium_fallback()

            data = resp.json()
            records = data.get('records', {}).get('data', [])

            # Get nearest expiry if not specified
            if not expiry_str:
                expiry_dates = data.get('records', {}).get('expiryDates', [])
                if expiry_dates:
                    expiry_str = expiry_dates[0]  # nearest expiry

            # Find matching strike and expiry
            for record in records:
                if (record.get('strikePrice') == strike and
                        record.get('expiryDate') == expiry_str):
                    option_data = record.get(option_type, {})
                    ltp = option_data.get('lastPrice', 0)
                    if ltp > 0:
                        logger.info("NSE premium: %s%d%s = Rs.%s",
                                    'NIFTY', strike, option_type, ltp)
                        # Store for premium ATR calculation
                        self._update_premium_cache(
                            str(strike) + option_type, ltp)
                        return float(ltp)

            logger.warning("Premium not found for %d%s. Using fallback.",
                           strike, option_type)
            return self._estimate_premium_fallback()

        except Exception as e:
            logger.warning("NSE option chain error: %s", e)
            return self._estimate_premium_fallback()

    def _update_premium_cache(self, key: str, price: float):
        """Store last 10 premium prices for ATR calculation."""
        if key not in PREMIUM_CACHE:
            PREMIUM_CACHE[key] = []
        PREMIUM_CACHE[key].append(price)
        PREMIUM_CACHE[key] = PREMIUM_CACHE[key][-10:]  # Keep last 10

    def get_premium_atr(self, key: str, period: int = 5) -> float:
        """
        Calculate ATR from last N premium prices.
        Used for adaptive premium SL.
        Returns ATR value or None if insufficient data.
        """
        prices = PREMIUM_CACHE.get(key, [])
        if len(prices) < 3:
            return None
        prices = prices[-period:]
        if len(prices) < 2:
            return None
        ranges = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
        return round(sum(ranges) / len(ranges), 2)

    def _estimate_premium_fallback(self, atr_value: float = None) -> float:
        """Fallback premium estimate when NSE is unavailable."""
        if atr_value:
            return round(max(50, min(300, atr_value * 0.4)), 0)
        return 120  # Reasonable default for ATM Nifty option

    # ── VIX FETCH ────────────────────────────────────────────
    def get_vix_from_kite(self, kite) -> dict:
        """Get India VIX from Kite Connect — more reliable than NSE scraping."""
        try:
            quote = kite.ltp(['NSE:INDIA VIX'])
            vix = float(quote['NSE:INDIA VIX']['last_price'])
            # Calculate ratio vs 10-day average
            history = getattr(self, '_vix_history', [])
            history.append(vix)
            if len(history) > 10:
                history = history[-10:]
            self._vix_history = history
            avg = sum(history) / len(history)
            ratio = round(vix / avg, 2) if avg > 0 else 1.0
            if ratio < 0.7:
                regime = 'SUPPRESSED'
            elif ratio <= 1.3:
                regime = 'NORMAL'
            elif ratio <= 1.5:
                regime = 'ELEVATED'
            else:
                regime = 'EXTREME'
            return {
                'current': vix,
                'average': round(avg, 2),
                'ratio': ratio,
                'regime': regime,
                'source': 'kite'
            }
        except Exception as e:
            logger.debug("Kite VIX error: %s", e)
            return {'current': 15.0, 'ratio': 1.0, 'regime': 'NORMAL', 'source': 'fallback'}

    def get_vix(self) -> dict:
        """
        Fetch India VIX from NSE.
        Returns dict with current VIX, 10-day avg, and ratio.
        Has retry logic, cache, and fallback.
        """
        vix_value = self._fetch_vix_with_retry()

        if vix_value:
            self._last_vix = vix_value
            self._update_vix_history(vix_value)

        current = self._last_vix or 15.0  # Fallback to neutral VIX
        avg_10d = self._vix_10d_avg or current

        ratio = round(current / avg_10d, 3) if avg_10d > 0 else 1.0

        return {
            'current': round(current, 2),
            'avg_10d': round(avg_10d, 2),
            'ratio': ratio,
            'regime': self._classify_vix_regime(ratio),
            'source': 'live' if vix_value else 'cached',
        }

    def _fetch_vix_with_retry(self, max_retries: int = 3) -> float:
        """Fetch VIX with retry logic."""
        for attempt in range(max_retries):
            try:
                session = self._get_nse_session()
                if not session:
                    time.sleep(2)
                    continue

                resp = session.get(
                    'https://www.nseindia.com/api/allIndices',
                    timeout=10)

                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get('data', []):
                        if item.get('index') == 'INDIA VIX':
                            vix = float(item.get('last', 0))
                            if vix > 0:
                                return vix

            except Exception as e:
                logger.debug("VIX fetch attempt %d failed: %s", attempt+1, e)
                time.sleep(2)

        logger.warning("VIX fetch failed after %d retries. Using cached.", max_retries)
        return None

    def _classify_vix_regime(self, ratio: float) -> str:
        """Classify VIX regime based on ratio to 10-day average."""
        if ratio < 0.7:
            return 'SUPPRESSED'
        elif ratio <= 1.3:
            return 'NORMAL'
        elif ratio <= 1.5:
            return 'ELEVATED'
        else:
            return 'PANIC'

    def _load_vix_history(self):
        """Load VIX history from file."""
        try:
            if os.path.exists(VIX_CACHE_FILE):
                with open(VIX_CACHE_FILE, 'r') as f:
                    data = json.load(f)
                history = data.get('history', [])
                if history:
                    self._last_vix = history[-1].get('vix', 15.0)
                    values = [h['vix'] for h in history[-10:]]
                    self._vix_10d_avg = sum(values) / len(values)
        except Exception as e:
            logger.debug("VIX history load failed: %s", e)

    def _update_vix_history(self, vix: float):
        """Update VIX history file with today's value."""
        try:
            history = []
            if os.path.exists(VIX_CACHE_FILE):
                with open(VIX_CACHE_FILE, 'r') as f:
                    data = json.load(f)
                history = data.get('history', [])

            today = str(date.today())
            # Update today's entry or add new
            if history and history[-1].get('date') == today:
                history[-1]['vix'] = vix
            else:
                history.append({'date': today, 'vix': vix})

            # Keep last 30 days
            history = history[-30:]

            # Recalculate 10-day average
            values = [h['vix'] for h in history[-10:]]
            self._vix_10d_avg = sum(values) / len(values)

            with open(VIX_CACHE_FILE, 'w') as f:
                json.dump({
                    'history': history,
                    'avg_10d': round(self._vix_10d_avg, 2),
                    'last_updated': str(datetime.now()),
                }, f, indent=2)

        except Exception as e:
            logger.debug("VIX history update failed: %s", e)

    # ── MARKET DATA ──────────────────────────────────────────
    def get_candles(self, symbol: str, interval: str,
                    count: int = 100) -> pd.DataFrame:
        """Fetch OHLCV candles from yfinance."""
        try:
            yf_map = {
                '1minute': '1m', '5minute': '5m',
                '15minute': '15m', '60minute': '60m',
            }
            yf_interval = yf_map.get(interval, '5m')
            period = '1d' if '1m' in yf_interval else '5d'
            if yf_interval in ['15m', '60m']:
                period = '5d'

            ticker = yf.Ticker(self.index_symbol)
            df = ticker.history(period=period, interval=yf_interval)

            if df.empty:
                logger.warning("No data for %s %s", symbol, interval)
                return pd.DataFrame()

            df.index = pd.to_datetime(df.index)
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)

            df.columns = [c.lower() for c in df.columns]
            df = df[['open', 'high', 'low', 'close', 'volume']].tail(count)
            return df

        except Exception as e:
            logger.error("Candle fetch error: %s", e)
            return pd.DataFrame()

    def get_live_price(self, symbol: str) -> float:
        """Get current Nifty price."""
        try:
            ticker = yf.Ticker(self.index_symbol)
            df = ticker.history(period='1d', interval='1m')
            if not df.empty:
                return float(df['Close'].iloc[-1])
            return 0.0
        except Exception as e:
            logger.error("Live price error: %s", e)
            return 0.0

    # ── MARKET OPEN CHECK ────────────────────────────────────
    def is_market_open(self) -> bool:
        """
        Check if NSE market is currently open.
        Checks: weekend + holiday + market hours.
        """
        now = datetime.now()

        # Weekend check
        if now.weekday() >= 5:
            return False

        # Holiday check
        try:
            from core.holiday_checker import get_holiday_checker
            if get_holiday_checker().is_holiday(now.date()):
                return False
        except Exception:
            pass

        # Market hours
        market_open = now.replace(hour=9, minute=15, second=0)
        market_close = now.replace(hour=15, minute=30, second=0)
        return market_open <= now <= market_close

    def get_vwap(self, df: pd.DataFrame) -> float:
        """Calculate VWAP from candle data."""
        try:
            if df.empty or 'volume' not in df.columns:
                return 0.0
            typical_price = (df['high'] + df['low'] + df['close']) / 3
            vwap = (typical_price * df['volume']).sum() / df['volume'].sum()
            return round(float(vwap), 2)
        except Exception:
            return 0.0
