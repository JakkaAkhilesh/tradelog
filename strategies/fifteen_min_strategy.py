# ============================================================
# 15 MINUTE STRATEGY
# ─────────────────────────────────────────────────────────────
# RULES:
# 1. Timeframe: 15 minutes
# 2. Indicators: 13 EMA, 50 EMA, 200 EMA
# 3. Entry window: 9:15 AM to 12:45 PM
# 4. Entry Case 1: Breakout of 50 EMA with definitive candle
#    + 200 EMA available as target within 1:3 RRR
# 5. Entry Case 2: Gap open above/below 50 EMA → wait for
#    retest of 50 or 13 EMA + definitive candle
# 6. Max SL: 45 Nifty points
# 7. RRR must be between 1:1.5 and 1:3 (ignore outside range)
# 8. SL to breakeven at 50% target
# 9. Trail 13 EMA at 80% target
# 10. Close day after 1 target hit
# 11. Close day after 2 SL hits
# ============================================================

import pandas as pd
from datetime import datetime, date
import logging
from core.indicators import Indicators

logger = logging.getLogger(__name__)
ind = Indicators()


class FifteenMinStrategy:

    def __init__(self, params: dict):
        self.params = params
        self.ema_fast = params.get('ema_fast', 13)
        self.ema_mid = params.get('ema_mid', 50)
        self.ema_slow = params.get('ema_slow', 200)
        self.entry_start = params.get('entry_start', '09:15')
        self.entry_end = params.get('entry_end', '12:45')
        self.max_sl_points = params.get('max_sl_points', 45)
        self.min_rrr = params.get('min_rrr', 2.0)
        self.max_rrr = params.get('max_rrr', 3.0)
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

        # Reset daily
        if self.last_signal_date != today:
            self.signal_fired_today = False

        if self.signal_fired_today:
            result['reason'] = '15MIN: Signal already fired today'
            return result

        # Time window check
        if current_time < self.entry_start or current_time > self.entry_end:
            result['reason'] = f'15MIN: Outside entry window ({current_time})'
            return result

        # Need at least 205 candles for EMA200 on 15-min timeframe.
        # With Zerodha feed we get 300 candles (~57 trading days).
        # With yfinance fallback (60d period) we get ~100+ candles.
        # 205 ensures EMA200 is fully warmed up in both cases.
        if len(df) < 205:
            result['reason'] = f'15MIN: Not enough candles ({len(df)})'
            return result

        try:
            current_price = float(df['close'].iloc[-1])
            ema13 = ind.ema_value(df, self.ema_fast)
            ema50 = ind.ema_value(df, self.ema_mid)
            ema200 = ind.ema_value(df, self.ema_slow)
            is_definitive = ind.is_definitive_candle(df, -1)
            is_bullish = ind.is_bullish_candle(df, -1)
            atr_val = ind.atr_value(df, 14)

        except Exception as e:
            result['reason'] = f'15MIN: Indicator error: {e}'
            return result

        result['conditions'] = {
            'price': round(current_price, 2),
            'ema13': round(ema13, 2),
            'ema50': round(ema50, 2),
            'ema200': round(ema200, 2),
            'is_definitive': is_definitive,
        }

        # ── BULLISH SETUP ────────────────────────────────────
        # Price breaks above 50 EMA with definitive candle
        # 200 EMA must be ABOVE current price as target
        prev_close = float(df['close'].iloc[-2])
        prev_was_below_50 = prev_close < ema50

        if (is_bullish and is_definitive and
                current_price > ema50 and prev_was_below_50):

            # 200 EMA must be available as target above
            if ema200 > current_price:
                sl = self._find_sl(df, 'BUY', ema50, ema13, atr_val)
                if sl is None:
                    result['reason'] = '15MIN: Could not find valid SL'
                    return result

                sl_points = current_price - sl
                if sl_points > self.max_sl_points:
                    result['reason'] = f'15MIN: SL too wide ({sl_points:.0f} > {self.max_sl_points})'
                    return result

                target = ema200
                rr = abs(target - current_price) / abs(current_price - sl)

                if rr < self.min_rrr:
                    result['reason'] = f'15MIN: RRR too low ({rr:.2f} < {self.min_rrr})'
                    return result
                if rr > self.max_rrr:
                    result['reason'] = f'15MIN: RRR too high ({rr:.2f} > {self.max_rrr})'
                    return result

                result.update({
                    'signal': True, 'direction': 'BUY',
                    'entry_price': round(current_price, 2),
                    'sl': round(sl, 2),
                    'target': round(target, 2),
                    'rr': round(rr, 2),
                    'reason': f'15MIN BUY: Price({current_price:.0f}) > EMA50({ema50:.0f}), '
                              f'Target=EMA200({ema200:.0f}), RR=1:{rr:.2f}'
                })
                self.signal_fired_today = True
                self.last_signal_date = today
                return result

        # ── BEARISH SETUP ────────────────────────────────────
        prev_was_above_50 = prev_close > ema50
        if (not is_bullish and is_definitive and
                current_price < ema50 and prev_was_above_50):

            if ema200 < current_price:
                sl = self._find_sl(df, 'SELL', ema50, ema13, atr_val)
                if sl is None:
                    result['reason'] = '15MIN: Could not find valid SL'
                    return result

                sl_points = sl - current_price
                if sl_points > self.max_sl_points:
                    result['reason'] = f'15MIN: SL too wide ({sl_points:.0f})'
                    return result

                target = ema200
                rr = abs(current_price - target) / abs(sl - current_price)

                if rr < self.min_rrr or rr > self.max_rrr:
                    result['reason'] = f'15MIN: RRR out of range ({rr:.2f})'
                    return result

                result.update({
                    'signal': True, 'direction': 'SELL',
                    'entry_price': round(current_price, 2),
                    'sl': round(sl, 2),
                    'target': round(target, 2),
                    'rr': round(rr, 2),
                    'reason': f'15MIN SELL: Price({current_price:.0f}) < EMA50({ema50:.0f}), '
                              f'Target=EMA200({ema200:.0f}), RR=1:{rr:.2f}'
                })
                self.signal_fired_today = True
                self.last_signal_date = today
                return result

        result['reason'] = (f'15MIN: No setup. P={current_price:.0f} '
                           f'EMA13={ema13:.0f} EMA50={ema50:.0f} EMA200={ema200:.0f}')
        return result

    def _find_sl(self, df, direction, ema50, ema13, atr):
        """Find logical SL within 45 points"""
        current_price = float(df['close'].iloc[-1])
        entry_candle_low = float(df['low'].iloc[-1])
        entry_candle_high = float(df['high'].iloc[-1])

        if direction == 'BUY':
            # Try: below entry candle, below 50 EMA, below 13 EMA
            candidates = [
                entry_candle_low - (atr * 0.2),
                ema50 - (atr * 0.3),
                ema13 - (atr * 0.3),
            ]
            valid = [s for s in candidates
                     if (current_price - s) <= self.max_sl_points and s < current_price]
            return max(valid) if valid else None
        else:
            candidates = [
                entry_candle_high + (atr * 0.2),
                ema50 + (atr * 0.3),
                ema13 + (atr * 0.3),
            ]
            valid = [s for s in candidates
                     if (s - current_price) <= self.max_sl_points and s > current_price]
            return min(valid) if valid else None

    def reset_daily(self):
        self.signal_fired_today = False
        logger.info("15MIN: Daily state reset")
