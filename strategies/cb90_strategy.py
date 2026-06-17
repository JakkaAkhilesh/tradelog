# ============================================================
# CB90 STRATEGY — CONSOLIDATION BOX 90
# ─────────────────────────────────────────────────────────────
# RULES (from Booming Bulls Academy PDF):
# 1. Timeframe: 5 minutes
# 2. Look for minimum 90-minute consolidation (>= 18 candles)
#    where the box height is <= 0.22% of price (Nifty)
# 3. Only look for entries AFTER 11:00 AM
# 4. ADX >= 25 to confirm trend strength
# 5. Breakout must be OUTSIDE current day's range (new day high/low)
# 6. Wait for ONE confirmation candle (DCC) after breakout
# 7. If no confirmation within 6 candles → ignore the trade
# 8. Direction: breakout above box → BUY, below box → SELL
# 9. SL: opposite box boundary
# 10. Target: 1:1.5 RR minimum
# 11. After target hit → trail with 9 EMA (exit when price
#     crosses 9 EMA) — same as NTS/TRAP extended trail logic
# ─────────────────────────────────────────────────────────────
# Backtest results (2023-01-02 to 2026-06-17, 1262 days):
# Trades: 125 | Win Rate: 44.0% | PF: 1.51 | P&L: +Rs.1,21,726
# Best performing new strategy — highest PF of all 7 tested
# ============================================================

import pandas as pd
import numpy as np
from datetime import datetime, date
import logging
from core.indicators import Indicators

logger = logging.getLogger(__name__)
ind = Indicators()

# CB90 constants
CB90_MIN_CANDLES = 18          # 90 minutes / 5min per candle
CB90_MAX_RANGE_PCT = 0.22      # Maximum box height as % of price
CB90_CONFIRMATION_TIMEOUT = 6  # Ignore if no DCC within 6 candles


class CB90Strategy:
    """
    CB90 — Consolidation Box 90 Strategy.
    Identifies institutional breakouts from prolonged tight
    consolidation zones and trades the momentum burst.
    """

    def __init__(self, params: dict):
        self.params = params
        self.entry_after = params.get('entry_after', '11:00')
        self.min_rrr = params.get('min_rrr', 1.5)
        self.adx_threshold = params.get('adx_threshold', 25)
        self.max_range_pct = params.get('max_range_pct', CB90_MAX_RANGE_PCT)
        self.min_candles = params.get('min_candles', CB90_MIN_CANDLES)
        self.confirmation_timeout = params.get('confirmation_timeout',
                                               CB90_CONFIRMATION_TIMEOUT)

        # Daily state
        self.signal_fired_today = False
        self.last_signal_date = None

        # Breakout tracking state
        self.breakout_candle_idx = None   # df index where breakout occurred
        self.breakout_direction = None    # 'BUY' or 'SELL'
        self.box_high = None              # upper boundary of confirmed box
        self.box_low = None               # lower boundary of confirmed box
        self.candles_since_breakout = 0

    def check_signal(self, df: pd.DataFrame) -> dict:
        """
        Main entry-point called every 5-min candle close.

        Returns dict matching the standard bot signal structure:
        {
            'signal': True/False,
            'direction': 'BUY' or 'SELL',
            'entry_price': float,
            'sl': float,
            'target': float,
            'rr': float,
            'reason': str,
            'conditions': dict
        }
        """
        result = {
            'signal': False,
            'direction': None,
            'entry_price': None,
            'sl': None,
            'target': None,
            'rr': None,
            'reason': '',
            'conditions': {}
        }

        now = datetime.now()
        today = now.date()
        current_time = now.strftime('%H:%M')

        # ── DAILY RESET ──────────────────────────────────────
        if self.last_signal_date != today:
            self.signal_fired_today = False
            self.last_signal_date = today
            self.breakout_candle_idx = None
            self.breakout_direction = None
            self.box_high = None
            self.box_low = None
            self.candles_since_breakout = 0

        # ── CHECK 1: Already fired today ─────────────────────
        if self.signal_fired_today:
            result['reason'] = 'CB90: Signal already fired today'
            return result

        # ── CHECK 2: Entry window (after 11:00 AM) ───────────
        if current_time < self.entry_after:
            result['reason'] = f'CB90: Waiting for {self.entry_after} (current={current_time})'
            return result

        # ── CHECK 3: Enough data ──────────────────────────────
        if len(df) < self.min_candles + 5:
            result['reason'] = f'CB90: Not enough candles ({len(df)})'
            return result

        current_price = float(df['close'].iloc[-1])
        current_high = float(df['high'].iloc[-1])
        current_low = float(df['low'].iloc[-1])

        # ── CHECK 4: ADX filter ───────────────────────────────
        try:
            adx_val = ind.adx_value(df, 14)
            atr_val = ind.atr_value(df, 14)
        except Exception as e:
            result['reason'] = f'CB90: Indicator error: {e}'
            return result

        # ── STATE: If awaiting confirmation after breakout ────
        if self.breakout_candle_idx is not None:
            self.candles_since_breakout += 1

            if self.candles_since_breakout > self.confirmation_timeout:
                # Timed out — reset and wait for next box
                logger.info("CB90: Confirmation timeout after %d candles, resetting",
                             self.candles_since_breakout)
                self._reset_breakout_state()
                result['reason'] = 'CB90: Confirmation timeout — waiting for next box'
                return result

            # Check for DCC (double candle confirmation)
            is_dcc = ind.double_candle_confirmation(
                df, 'bullish' if self.breakout_direction == 'BUY' else 'bearish'
            )

            if not is_dcc:
                result['reason'] = (
                    f'CB90: Waiting for DCC confirmation '
                    f'({self.candles_since_breakout}/{self.confirmation_timeout} candles)'
                )
                result['conditions'] = {
                    'breakout_dir': self.breakout_direction,
                    'box_high': round(self.box_high, 2),
                    'box_low': round(self.box_low, 2),
                    'candles_since_breakout': self.candles_since_breakout,
                }
                return result

            # ── DCC CONFIRMED → GENERATE SIGNAL ──────────────
            direction = self.breakout_direction
            entry = current_price

            if direction == 'BUY':
                sl = round(self.box_low, 2)      # opposite box boundary
                risk = entry - sl
            else:
                sl = round(self.box_high, 2)     # opposite box boundary
                risk = sl - entry

            if risk <= 0:
                logger.warning("CB90: Invalid risk (%.2f) — resetting", risk)
                self._reset_breakout_state()
                result['reason'] = 'CB90: Invalid risk after confirmation — reset'
                return result

            target = round(entry + risk * self.min_rrr, 2) if direction == 'BUY' \
                else round(entry - risk * self.min_rrr, 2)
            rr = round(abs(target - entry) / risk, 2)

            if rr < self.min_rrr:
                self._reset_breakout_state()
                result['reason'] = f'CB90: RR {rr:.2f} below minimum {self.min_rrr}'
                return result

            result.update({
                'signal': True,
                'direction': direction,
                'entry_price': round(entry, 2),
                'sl': sl,
                'target': target,
                'rr': rr,
                'reason': (
                    f'CB90 {direction}: Box breakout confirmed with DCC | '
                    f'Box: {self.box_low:.0f}-{self.box_high:.0f} | '
                    f'SL: {sl} | Target: {target} | RR: 1:{rr}'
                ),
                'conditions': {
                    'adx': round(adx_val, 1),
                    'box_high': round(self.box_high, 2),
                    'box_low': round(self.box_low, 2),
                    'box_range_pct': round(
                        (self.box_high - self.box_low) / current_price * 100, 3
                    ),
                    'entry': round(entry, 2),
                    'sl': sl,
                    'target': target,
                    'rr': rr,
                    'candles_to_confirm': self.candles_since_breakout,
                }
            })
            self.signal_fired_today = True
            self.last_signal_date = today
            self._reset_breakout_state()
            logger.info("CB90: Signal generated — %s at %.2f SL=%.2f Target=%.2f RR=1:%.2f",
                         direction, entry, sl, target, rr)
            return result

        # ── NO PENDING BREAKOUT: look for a new box + breakout ──

        # ADX must be >= 25
        if adx_val < self.adx_threshold:
            result['reason'] = f'CB90: ADX too low ({adx_val:.1f} < {self.adx_threshold})'
            return result

        # Find consolidation box in recent candles
        box = self._find_box(df, current_price)
        if box is None:
            result['reason'] = 'CB90: No valid consolidation box found'
            result['conditions'] = {'adx': round(adx_val, 1), 'price': round(current_price, 2)}
            return result

        box_high, box_low = box

        # Current day high/low (up to but NOT including this candle)
        today_df = df[df.index.date == today]
        if len(today_df) < 2:
            result['reason'] = 'CB90: Insufficient today data'
            return result
        day_high = float(today_df['high'].iloc[:-1].max())
        day_low = float(today_df['low'].iloc[:-1].min())

        # Breakout must exceed the current day's high/low (not just box)
        breakout_dir = None
        if current_high > box_high and current_high > day_high:
            breakout_dir = 'BUY'
        elif current_low < box_low and current_low < day_low:
            breakout_dir = 'SELL'

        if breakout_dir is None:
            result['reason'] = (
                f'CB90: Box found ({box_low:.0f}-{box_high:.0f}) but '
                f'no breakout beyond day range yet '
                f'(day H={day_high:.0f} L={day_low:.0f})'
            )
            result['conditions'] = {
                'adx': round(adx_val, 1),
                'box_high': round(box_high, 2),
                'box_low': round(box_low, 2),
                'day_high': round(day_high, 2),
                'day_low': round(day_low, 2),
            }
            return result

        # ── BREAKOUT DETECTED → start waiting for DCC ────────
        self.breakout_candle_idx = len(df) - 1
        self.breakout_direction = breakout_dir
        self.box_high = box_high
        self.box_low = box_low
        self.candles_since_breakout = 0

        logger.info("CB90: Breakout detected — %s | Box: %.0f-%.0f | "
                     "Price: %.2f | Waiting for DCC (up to %d candles)",
                     breakout_dir, box_low, box_high, current_price,
                     self.confirmation_timeout)

        result['reason'] = (
            f'CB90: {breakout_dir} breakout detected at {current_price:.2f} | '
            f'Box: {box_low:.0f}-{box_high:.0f} | '
            f'Waiting for DCC (1/{self.confirmation_timeout} candles)'
        )
        result['conditions'] = {
            'adx': round(adx_val, 1),
            'box_high': round(box_high, 2),
            'box_low': round(box_low, 2),
        }
        return result

    def _find_box(self, df: pd.DataFrame, current_price: float):
        """
        Scan backward from the previous candle to find the longest
        consecutive run of candles whose high-low range is within
        CB90_MAX_RANGE_PCT of current price AND run >= CB90_MIN_CANDLES.

        Returns (box_high, box_low) or None.
        Uses numpy for speed (avoids pandas .iloc in inner loop).
        """
        highs = df['high'].to_numpy()
        lows = df['low'].to_numpy()
        n = len(df)

        if n < self.min_candles + 2:
            return None

        max_range = current_price * self.max_range_pct / 100
        max_lookback = min(n - 1, 60)  # cap at 60 candles back

        # Slice: candles BEFORE the current one (exclude index -1)
        window_hi = highs[n - 1 - max_lookback: n - 1]
        window_lo = lows[n - 1 - max_lookback: n - 1]

        # Cumulative max/min from the END of the window backward
        rev_hi = window_hi[::-1]
        rev_lo = window_lo[::-1]
        cum_hi = np.maximum.accumulate(rev_hi)
        cum_lo = np.minimum.accumulate(rev_lo)
        cum_range = cum_hi - cum_lo

        within = cum_range <= max_range
        if not within.any() or not within[0]:
            return None

        # Count the longest contiguous run from index 0
        max_len = 0
        for v in within:
            if v:
                max_len += 1
            else:
                break

        if max_len < self.min_candles:
            return None

        box_high = float(cum_hi[max_len - 1])
        box_low = float(cum_lo[max_len - 1])
        return box_high, box_low

    def _reset_breakout_state(self):
        """Reset breakout tracking — called after confirmation or timeout."""
        self.breakout_candle_idx = None
        self.breakout_direction = None
        self.box_high = None
        self.box_low = None
        self.candles_since_breakout = 0

    def reset_daily(self):
        """Called by main.py at day end / bot restart."""
        self.signal_fired_today = False
        self._reset_breakout_state()
        logger.info("CB90: Daily state reset")
