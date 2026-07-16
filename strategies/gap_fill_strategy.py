# ============================================================
# GAP FILL STRATEGY — FIXED VERSION
# SL: 2x ATR (wider, realistic)
# Target: always 1:3 RR
# Trail: 9 EMA activates after 80% target (in main.py monitor)
# ============================================================

import pandas as pd
from datetime import datetime, date
import logging
from core.indicators import Indicators

logger = logging.getLogger(__name__)
ind = Indicators()


class GapFillStrategy:

    def __init__(self, params: dict):
        self.params = params
        self.max_gap_pct = params.get('max_gap_pct', 1.0)
        self.min_pdc_distance_pct = params.get('min_pdc_distance_pct', 0.20)
        self.first_half_end = params.get('first_half_end', '10:00')
        self.second_half_start = params.get('second_half_start', '13:00')
        self.second_half_end = params.get('second_half_end', '14:30')
        self.signal_fired_today = False
        self.last_signal_date = None
        self.first_half_fired = False
        self.gap_direction = None

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
            self.first_half_fired = False
            self.gap_direction = None

        # Allow second half even if first half fired
        in_second_half = (self.second_half_start <= current_time <= self.second_half_end)
        if self.signal_fired_today and not in_second_half:
            result['reason'] = 'GAP: Day complete'
            return result
        if self.signal_fired_today and in_second_half and self.first_half_fired:
            # Second half allowed only if first half actually traded (not just attempted)
            pass  # Allow second half check

        if len(df) < 5:
            result['reason'] = 'GAP: Not enough data'
            return result

        try:
            pdc = ind.prev_day_close(df)
            if pdc == 0:
                result['reason'] = 'GAP: Could not get PDC'
                return result
            current_price = float(df['close'].iloc[-1])
            today_open = self._get_today_open(df, today)
            if today_open is None:
                result['reason'] = 'GAP: No today open found'
                return result
            gap_pct = ((today_open - pdc) / pdc) * 100
            atr_val = ind.atr_value(df, 14)
        except Exception as e:
            result['reason'] = f'GAP: Error: {e}'
            return result

        if abs(gap_pct) < 0.1:
            result['reason'] = f'GAP: No gap today (gap={gap_pct:.2f}%)'
            return result
        if abs(gap_pct) > self.max_gap_pct:
            result['reason'] = f'GAP: Gap too large ({gap_pct:.2f}%)'
            return result

        gap_up = gap_pct > 0
        self.gap_direction = 'UP' if gap_up else 'DOWN'
        pdc_dist = abs((current_price - pdc) / pdc) * 100

        result['conditions'] = {
            'pdc': round(pdc, 2), 'today_open': round(today_open, 2),
            'gap_pct': round(gap_pct, 2), 'gap_direction': self.gap_direction,
            'current_price': round(current_price, 2), 'pdc_dist_pct': round(pdc_dist, 2),
        }

        # ── FIRST HALF (before 10 AM) ─────────────────────
        if current_time < self.first_half_end and not self.first_half_fired:
            fc = self._get_first_candle(df, today)
            if fc is None:
                result['reason'] = 'GAP: Waiting for first candle'
                return result
            fc_high = float(fc['high'])
            fc_low = float(fc['low'])

            # GAP UP → sell when price breaks below first candle
            if gap_up and current_price < fc_low and pdc_dist >= self.min_pdc_distance_pct:
                sl = round(current_price + (atr_val * 1.0), 2)
                risk = abs(sl - current_price)
                target = round(current_price - (risk * 3.0), 2)
                result.update({
                    'signal': True, 'direction': 'SELL',
                    'entry_price': round(current_price, 2),
                    'sl': sl, 'target': target, 'rr': 3.0,
                    'reason': f'GAP SELL 1H: Gap+{gap_pct:.2f}% broke below 1st candle. SL={sl} Tgt={target} RR=1:3'
                })
                self.signal_fired_today = True
                self.first_half_fired = True
                self.last_signal_date = today
                return result

            # GAP DOWN → buy when price breaks above first candle
            elif not gap_up and current_price > fc_high and pdc_dist >= self.min_pdc_distance_pct:
                sl = round(current_price - (atr_val * 1.0), 2)
                risk = abs(current_price - sl)
                target = round(current_price + (risk * 3.0), 2)
                result.update({
                    'signal': True, 'direction': 'BUY',
                    'entry_price': round(current_price, 2),
                    'sl': sl, 'target': target, 'rr': 3.0,
                    'reason': f'GAP BUY 1H: Gap-{abs(gap_pct):.2f}% broke above 1st candle. SL={sl} Tgt={target} RR=1:3'
                })
                self.signal_fired_today = True
                self.first_half_fired = True
                self.last_signal_date = today
                return result

            result['reason'] = f'GAP: Watching 1H. Gap={gap_pct:.2f}% 1C-H={fc_high:.0f} 1C-L={fc_low:.0f} P={current_price:.0f}'
            return result

        # ── SECOND HALF (1 PM to 2:30 PM) ────────────────
        if self.second_half_start <= current_time <= self.second_half_end and not self.first_half_fired:
            is_bull = ind.double_candle_confirmation(df, 'bullish')
            is_bear = ind.double_candle_confirmation(df, 'bearish')

            if gap_up and is_bear and pdc_dist >= self.min_pdc_distance_pct:
                sl = round(current_price + (atr_val * 1.0), 2)
                risk = abs(sl - current_price)
                target = round(current_price - (risk * 3.0), 2)
                result.update({
                    'signal': True, 'direction': 'SELL',
                    'entry_price': round(current_price, 2),
                    'sl': sl, 'target': target, 'rr': 3.0,
                    'reason': f'GAP SELL 2H: Gap+{gap_pct:.2f}% DCC bear. SL={sl} Tgt={target} RR=1:3'
                })
                self.signal_fired_today = True
                self.last_signal_date = today
                return result

            elif not gap_up and is_bull and pdc_dist >= self.min_pdc_distance_pct:
                sl = round(current_price - (atr_val * 1.0), 2)
                risk = abs(current_price - sl)
                target = round(current_price + (risk * 3.0), 2)
                result.update({
                    'signal': True, 'direction': 'BUY',
                    'entry_price': round(current_price, 2),
                    'sl': sl, 'target': target, 'rr': 3.0,
                    'reason': f'GAP BUY 2H: Gap-{abs(gap_pct):.2f}% DCC bull. SL={sl} Tgt={target} RR=1:3'
                })
                self.signal_fired_today = True
                self.last_signal_date = today
                return result

        result['reason'] = f'GAP: No setup. Gap={gap_pct:.2f}% Time={current_time}'
        return result

    def _get_today_open(self, df, today):
        tc = df[df.index.date == today]
        return float(tc['open'].iloc[0]) if not tc.empty else None

    def _get_first_candle(self, df, today):
        tc = df[df.index.date == today]
        return tc.iloc[0] if not tc.empty else None

    def reset_daily(self):
        self.signal_fired_today = False
        self.first_half_fired = False
        self.gap_direction = None
        logger.info("GAP: Daily state reset")
