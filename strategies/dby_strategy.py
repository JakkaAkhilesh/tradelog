# ============================================================
# DBY STRATEGY — FIXED VERSION
# Changes: Tighter SL (0.15x ATR) to keep risk within Rs.500
# ============================================================

import pandas as pd
from datetime import datetime, date
import logging
from core.indicators import Indicators

logger = logging.getLogger(__name__)
ind = Indicators()


class DbyStrategy:

    def __init__(self, params: dict):
        self.params = params
        self.entry_end = params.get('entry_end', '10:00')
        self.min_rrr = params.get('min_rrr', 2.0)
        self.signal_fired_today = False
        self.last_signal_date = None

    def check_signal(self, df: pd.DataFrame) -> dict:
        result = {
            'signal': False, 'direction': None,
            'entry_price': None, 'sl': None,
            'target': None, 'rr': None,
            'reason': '', 'conditions': {}
        }

        now = datetime.now()
        today = now.date()
        current_time = now.strftime('%H:%M')

        if self.last_signal_date != today:
            self.signal_fired_today = False

        if self.signal_fired_today:
            result['reason'] = 'DBY: Signal already fired today'
            return result

        if current_time >= self.entry_end:
            result['reason'] = 'DBY: Entry window closed at ' + self.entry_end
            return result

        if current_time < '09:15':
            result['reason'] = 'DBY: Waiting for market open'
            return result

        if len(df) < 10:
            result['reason'] = 'DBY: Not enough data'
            return result

        try:
            current_price = float(df['close'].iloc[-1])
            atr_val = ind.atr_value(df, 14)
            is_definitive = ind.is_definitive_candle(df, -1)
            is_bullish = ind.is_bullish_candle(df, -1)
            prev2_high, prev2_low = self._get_prev2_days_hl(df, today)
            if prev2_high is None or prev2_low is None:
                result['reason'] = 'DBY: Could not get 2-day H/L'
                return result
        except Exception as e:
            result['reason'] = 'DBY: Error: ' + str(e)
            return result

        result['conditions'] = {
            'price': round(current_price, 2),
            'prev2_high': round(prev2_high, 2),
            'prev2_low': round(prev2_low, 2),
            'is_definitive': is_definitive,
        }

        # FIXED: Tighter SL (0.15x ATR) to keep risk within Rs.500
        # Previous was 0.3x ATR which gave Rs.2486 loss today

        # BULLISH: Break above 2-day high
        if current_price > prev2_high and is_definitive and is_bullish:
            sl = round(prev2_high - (atr_val * 0.5), 2)
            risk = current_price - sl
            target = round(current_price + (risk * self.min_rrr * 1.2), 2)
            rr = round(abs(target - current_price) / abs(current_price - sl), 2)
            if rr >= self.min_rrr:
                result.update({
                    'signal': True, 'direction': 'BUY',
                    'entry_price': round(current_price, 2),
                    'sl': sl, 'target': target, 'rr': rr,
                    'reason': ('DBY BUY: Price(' + str(round(current_price)) +
                               ') broke 2-day high(' + str(round(prev2_high)) +
                               '), RR=1:' + str(rr))
                })
                self.signal_fired_today = True
                self.last_signal_date = today
                return result

        # BEARISH: Break below 2-day low
        if current_price < prev2_low and is_definitive and not is_bullish:
            sl = round(prev2_low + (atr_val * 0.5), 2)
            risk = sl - current_price
            target = round(current_price - (risk * self.min_rrr * 1.2), 2)
            rr = round(abs(current_price - target) / abs(sl - current_price), 2)
            if rr >= self.min_rrr:
                result.update({
                    'signal': True, 'direction': 'SELL',
                    'entry_price': round(current_price, 2),
                    'sl': sl, 'target': target, 'rr': rr,
                    'reason': ('DBY SELL: Price(' + str(round(current_price)) +
                               ') broke 2-day low(' + str(round(prev2_low)) +
                               '), RR=1:' + str(rr))
                })
                self.signal_fired_today = True
                self.last_signal_date = today
                return result

        result['reason'] = ('DBY: Watching. Price=' + str(round(current_price)) +
                            ' 2D-High=' + str(round(prev2_high)) +
                            ' 2D-Low=' + str(round(prev2_low)))
        return result

    def _get_prev2_days_hl(self, df, today):
        try:
            daily = df.resample('D').agg({
                'open': 'first', 'high': 'max',
                'low': 'min', 'close': 'last'
            }).dropna()
            past = daily[daily.index.date < today]
            if len(past) < 2:
                return None, None
            prev2 = past.tail(2)
            high = max(
                max(prev2.iloc[0]['open'], prev2.iloc[0]['close']),
                max(prev2.iloc[1]['open'], prev2.iloc[1]['close'])
            )
            low = min(
                min(prev2.iloc[0]['open'], prev2.iloc[0]['close']),
                min(prev2.iloc[1]['open'], prev2.iloc[1]['close'])
            )
            return high, low
        except Exception as e:
            logger.error("DBY: Error getting prev 2 days H/L: %s", e)
            return None, None

    def reset_daily(self):
        self.signal_fired_today = False
        logger.info("DBY: Daily state reset")
