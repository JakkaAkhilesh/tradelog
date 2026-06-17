# ============================================================
# TRAP TRADING STRATEGY
# (Previously called RAP Trading)
# ─────────────────────────────────────────────────────────────
# RULES:
# 1. After 10:00 AM mark Current Day High (CDH) and Low (CDL)
# 2. If price breaks above CDH or below CDL and retraces back
#    inside that level → entry with Double Candle Confirmation
# 3. If entry candle is big engulfing → enter on same candle
# 4. If no confirmation within 8 candles → ignore the trade
# 5. Entries only till 12:00 PM
# 6. Minimum RRR 1:1.5
# ============================================================

import pandas as pd
from datetime import datetime, time, date
import logging
from core.indicators import Indicators

logger = logging.getLogger(__name__)
ind = Indicators()


class TrapTrading:

    def __init__(self, params: dict):
        self.params = params
        self.mark_after = params.get('mark_cdh_cdl_after', '10:00')
        self.entry_end = params.get('entry_end', '12:00')
        self.timeout_candles = params.get('timeout_candles', 8)
        self.min_rrr = params.get('min_rrr', 2.0)
        self.signal_fired_today = False
        self.last_signal_date = None
        self.cdh = None
        self.cdl = None
        self.cdh_marked_date = None
        self.breakout_candle_count = 0
        self.watching_for_retrace = None

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

        # Reset daily state
        if self.last_signal_date != today:
            self.signal_fired_today = False
            self.cdh = None
            self.cdl = None
            self.watching_for_retrace = None
            self.breakout_candle_count = 0

        # Already fired today
        if self.signal_fired_today:
            result['reason'] = 'TRAP: Signal already fired today'
            return result

        # Entry window check (after 10 AM, before 12 PM)
        if current_time < self.mark_after:
            result['reason'] = f'TRAP: Waiting for 10:00 AM (current={current_time})'
            return result

        if current_time >= self.entry_end:
            result['reason'] = f'TRAP: Entry window closed at {self.entry_end}'
            return result

        if len(df) < 20:
            result['reason'] = 'TRAP: Not enough data'
            return result

        current_price = float(df['close'].iloc[-1])
        atr_val = ind.atr_value(df, 14)

        # Mark CDH/CDL from candles after 10 AM today
        today_df = df[df.index.date == today]
        ten_am = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        after_10 = today_df[today_df.index >= ten_am]

        if after_10.empty:
            result['reason'] = 'TRAP: No candles after 10 AM yet'
            return result

        self.cdh = float(after_10['high'].max())
        self.cdl = float(after_10['low'].min())

        result['conditions'] = {
            'price': round(current_price, 2),
            'cdh': round(self.cdh, 2),
            'cdl': round(self.cdl, 2),
            'time': current_time,
        }

        # Check last few candles for breakout and retrace
        recent = df.tail(10)

        # BULLISH TRAP: Price broke above CDH and came back below
        for i in range(len(recent) - 2, 0, -1):
            candle = recent.iloc[i]
            if candle['high'] > self.cdh:
                # Found breakout candle — check if current price retraced
                if current_price < self.cdh:
                    # Retraced back inside
                    is_dcc = ind.double_candle_confirmation(df, 'bullish')
                    is_engulf = ind.is_engulfing(df)
                    if is_dcc or is_engulf:
                        sl = current_price - (atr_val * 1.5)
                        target = current_price + (atr_val * 1.5 * self.min_rrr)
                        rr = abs(target - current_price) / abs(current_price - sl)
                        if rr >= self.min_rrr:
                            result.update({
                                'signal': True, 'direction': 'BUY',
                                'entry_price': round(current_price, 2),
                                'sl': round(sl, 2),
                                'target': round(target, 2),
                                'rr': round(rr, 2),
                                'reason': f'TRAP BUY: Break above CDH({self.cdh:.0f}) + retrace + {"DCC" if is_dcc else "Engulf"}'
                            })
                            self.signal_fired_today = True
                            self.last_signal_date = today
                            return result
                break

        # BEARISH TRAP: Price broke below CDL and came back above
        for i in range(len(recent) - 2, 0, -1):
            candle = recent.iloc[i]
            if candle['low'] < self.cdl:
                if current_price > self.cdl:
                    is_dcc = ind.double_candle_confirmation(df, 'bearish')
                    is_engulf = ind.is_engulfing(df)
                    if is_dcc or is_engulf:
                        sl = current_price + (atr_val * 1.5)
                        target = current_price - (atr_val * 1.5 * self.min_rrr)
                        rr = abs(target - current_price) / abs(sl - current_price)
                        if rr >= self.min_rrr:
                            result.update({
                                'signal': True, 'direction': 'SELL',
                                'entry_price': round(current_price, 2),
                                'sl': round(sl, 2),
                                'target': round(target, 2),
                                'rr': round(rr, 2),
                                'reason': f'TRAP SELL: Break below CDL({self.cdl:.0f}) + retrace + {"DCC" if is_dcc else "Engulf"}'
                            })
                            self.signal_fired_today = True
                            self.last_signal_date = today
                            return result
                break

        result['reason'] = f'TRAP: Watching CDH={self.cdh:.0f} CDL={self.cdl:.0f} Price={current_price:.0f}'
        return result

    def reset_daily(self):
        self.signal_fired_today = False
        self.cdh = None
        self.cdl = None
        self.watching_for_retrace = None
        self.breakout_candle_count = 0
        logger.info("TRAP: Daily state reset")
