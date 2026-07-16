# ============================================================
# NIFTY TRIPLE SYNC STRATEGY (NTS)
# Win Rate: 64.94% | Best automation score: 5/5
# ─────────────────────────────────────────────────────────────
# RULES:
# 1. Timeframe: 5 minutes
# 2. Days: Monday to Thursday ONLY
# 3. Entry slots: 9:15, 9:35, 9:45-10:05 candle closes
# 4. ALL THREE must align:
#    - Price above/below 200 EMA
#    - Supertrend(10,2) green/red
#    - ADX > 25
# 5. Bullish entry: price above EMA + ST green + definitive green candle
# 6. Bearish entry: price below EMA + ST red + definitive red candle
# 7. If all conditions satisfied before 9:30 AM → SKIP that day
# 8. Minimum RRR: 1:1.5
# ============================================================

import pandas as pd
from datetime import datetime, time
import logging
from core.indicators import Indicators

logger = logging.getLogger(__name__)
ind = Indicators()


class NiftyTripleSync:
    """
    Primary strategy — Nifty Triple Sync (NTS).
    Highest win rate (64.94%) and easiest to automate.
    """

    def __init__(self, params: dict):
        self.params = params
        self.ema_period = params.get('ema_period', 200)
        self.adx_period = params.get('adx_period', 14)
        self.adx_threshold = params.get('adx_threshold', 25)
        self.st_atr = params.get('supertrend_atr', 10)
        self.st_factor = params.get('supertrend_factor', 2)
        self.entry_slots = params.get('entry_slots',
                           ['09:15','09:35','09:45','09:50',
                            '09:55','10:00','10:05'])
        self.active_days = params.get('active_days', [0,1,2,3])
        self.min_rrr = params.get('min_rrr', 2.0)

        # State tracking
        self.conditions_met_at_open = False
        self.signal_fired_today = False
        self.last_signal_date = None

    def check_signal(self, df: pd.DataFrame) -> dict:
        """
        Main method — checks all NTS conditions.

        Returns dict with:
        {
            'signal': True/False,
            'direction': 'BUY' or 'SELL',
            'entry_price': float,
            'sl': float,
            'target': float,
            'rr': float,
            'reason': str,
            'conditions': dict  ← all individual checks
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

        # ── CHECK 1: Trading day ─────────────────────────
        now = datetime.now()
        today = now.weekday()  # 0=Mon, 6=Sun

        if today not in self.active_days:
            result['reason'] = f'NTS: Not active today (day={today}, active={self.active_days})'
            return result

        # ── CHECK 2: Already fired today ────────────────
        today_date = now.date()
        if self.signal_fired_today and self.last_signal_date == today_date:
            result['reason'] = 'NTS: Signal already fired today'
            return result

        # ── CHECK 3: Entry time slot ─────────────────────
        current_time_str = now.strftime('%H:%M')
        in_entry_slot = self._is_in_entry_slot(current_time_str)

        if not in_entry_slot:
            result['reason'] = f'NTS: Not in entry slot (current={current_time_str})'
            return result

        # ── CHECK 4: Enough data ─────────────────────────
        if len(df) < self.ema_period + 10:
            result['reason'] = f'NTS: Not enough candles ({len(df)})'
            return result

        # ── CALCULATE INDICATORS ─────────────────────────
        try:
            current_price = float(df['close'].iloc[-1])
            ema200 = ind.ema_value(df, self.ema_period)
            adx_val = ind.adx_value(df, self.adx_period)
            st_bullish = ind.supertrend_bullish(df, self.st_atr, self.st_factor)
            is_definitive = ind.is_definitive_candle(df, -1)
            is_bullish_candle = ind.is_bullish_candle(df, -1)
            atr_val = ind.atr_value(df, 14)

        except Exception as e:
            result['reason'] = f'NTS: Indicator calculation error: {e}'
            return result

        # Store all conditions for transparency
        price_above_ema = current_price > ema200
        adx_strong = adx_val > self.adx_threshold

        result['conditions'] = {
            'price': round(current_price, 2),
            'ema200': round(ema200, 2),
            'price_above_ema': price_above_ema,
            'adx': round(adx_val, 2),
            'adx_strong': adx_strong,
            'supertrend_bullish': st_bullish,
            'is_definitive_candle': is_definitive,
            'is_bullish_candle': is_bullish_candle,
            'time': current_time_str,
        }

        # ── CHECK 5: ADX threshold ───────────────────────
        if not adx_strong:
            result['reason'] = f'NTS: ADX too weak ({adx_val:.1f} < {self.adx_threshold})'
            return result

        # ── CHECK 6: BULLISH SIGNAL ──────────────────────
        # All three must align: Above EMA + ST Green + Definitive Green Candle
        if (price_above_ema and st_bullish and
                is_definitive and is_bullish_candle):

            # Calculate SL and Target
            sl = self._calculate_sl(df, 'BUY', atr_val)
            target = self._calculate_target(current_price, sl, 'BUY',
                                             self.min_rrr)
            rr = self._calculate_rr(current_price, sl, target)

            if rr < self.min_rrr:
                result['reason'] = f'NTS: BUY signal but RR too low ({rr:.2f})'
                return result

            result.update({
                'signal': True,
                'direction': 'BUY',
                'entry_price': round(current_price, 2),
                'sl': round(sl, 2),
                'target': round(target, 2),
                'rr': round(rr, 2),
                'reason': f'NTS BUY: Price({current_price:.0f}) > EMA200({ema200:.0f}), '
                          f'ST=Green, ADX={adx_val:.1f}, RR=1:{rr:.2f}',
            })
            self.signal_fired_today = True
            self.last_signal_date = today_date
            return result

        # ── CHECK 7: BEARISH SIGNAL ──────────────────────
        # All three must align: Below EMA + ST Red + Definitive Red Candle
        if (not price_above_ema and not st_bullish and
                is_definitive and not is_bullish_candle):

            sl = self._calculate_sl(df, 'SELL', atr_val)
            target = self._calculate_target(current_price, sl, 'SELL',
                                             self.min_rrr)
            rr = self._calculate_rr(current_price, sl, target)

            if rr < self.min_rrr:
                result['reason'] = f'NTS: SELL signal but RR too low ({rr:.2f})'
                return result

            result.update({
                'signal': True,
                'direction': 'SELL',
                'entry_price': round(current_price, 2),
                'sl': round(sl, 2),
                'target': round(target, 2),
                'rr': round(rr, 2),
                'reason': f'NTS SELL: Price({current_price:.0f}) < EMA200({ema200:.0f}), '
                          f'ST=Red, ADX={adx_val:.1f}, RR=1:{rr:.2f}',
            })
            self.signal_fired_today = True
            self.last_signal_date = today_date
            return result

        # ── NO SIGNAL ────────────────────────────────────
        alignment = []
        if price_above_ema: alignment.append('EMA✅')
        else: alignment.append('EMA❌')
        if st_bullish: alignment.append('ST✅')
        else: alignment.append('ST❌')
        if adx_strong: alignment.append('ADX✅')
        else: alignment.append('ADX❌')

        result['reason'] = f'NTS: No full alignment — {" ".join(alignment)}'
        return result

    # ── STOP LOSS CALCULATION ────────────────────────────────
    def _calculate_sl(self, df: pd.DataFrame, direction: str,
                      atr_val: float) -> float:
        """
        SL placement for NTS:
        - BUY: Below the low of the entry candle or 1.5x ATR below entry
        - SELL: Above the high of the entry candle or 1.5x ATR above entry
        """
        entry_candle = df.iloc[-1]
        atr_sl = atr_val * 1.5

        if direction == 'BUY':
            candle_sl = entry_candle['low'] - (atr_val * 0.3)
            return min(entry_candle['close'] - atr_sl,
                      entry_candle['close'] - (entry_candle['close'] - candle_sl))
        else:
            candle_sl = entry_candle['high'] + (atr_val * 0.3)
            return max(entry_candle['close'] + atr_sl,
                      entry_candle['close'] + (candle_sl - entry_candle['close']))

    def _calculate_target(self, entry: float, sl: float,
                          direction: str, min_rrr: float) -> float:
        """Calculate minimum target based on RRR"""
        risk = abs(entry - sl)
        reward = risk * min_rrr * 1.2  # Give 20% buffer above minimum
        if direction == 'BUY':
            return entry + reward
        else:
            return entry - reward

    def _calculate_rr(self, entry: float, sl: float,
                      target: float) -> float:
        """Calculate Risk:Reward ratio"""
        risk = abs(entry - sl)
        reward = abs(target - entry)
        return reward / risk if risk > 0 else 0

    def _is_in_entry_slot(self, current_time: str) -> bool:
        """Check if current time is within any entry slot"""
        # Entry slots: 09:15, 09:35, 09:45-10:05
        # We check within a 5-minute window of each slot
        slot_windows = [
            ('09:15', '09:20'),
            ('09:35', '09:40'),
            ('09:45', '10:10'),  # Extended window 9:45-10:05
        ]
        curr = datetime.strptime(current_time, '%H:%M').time()
        for start_str, end_str in slot_windows:
            start = datetime.strptime(start_str, '%H:%M').time()
            end = datetime.strptime(end_str, '%H:%M').time()
            if start <= curr <= end:
                return True
        return False

    def reset_daily(self):
        """Call this at market close or start of new day"""
        self.conditions_met_at_open = False
        self.signal_fired_today = False
        logger.info("NTS: Daily state reset")

    def get_status(self) -> dict:
        """Get current strategy status"""
        now = datetime.now()
        return {
            'strategy': 'Nifty Triple Sync',
            'active_today': now.weekday() in self.active_days,
            'signal_fired': self.signal_fired_today,
            'last_signal_date': str(self.last_signal_date),
        }


# ── STANDALONE TEST ─────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.path.append('..')
    from core.data_fetcher import DataFetcher
    from config.settings import DATA, INDEX, STRATEGY_PARAMS

    settings = {"DATA": DATA, "INDEX": INDEX}
    fetcher = DataFetcher(settings)

    print("Testing Nifty Triple Sync Strategy...")
    df = fetcher.get_candles("NIFTY50", "5minute", 250)

    if not df.empty:
        strategy = NiftyTripleSync(STRATEGY_PARAMS['nifty_triple_sync'])
        result = strategy.check_signal(df)

        print(f"\n{'='*50}")
        print(f"Signal: {'✅ YES' if result['signal'] else '❌ NO'}")
        print(f"Direction: {result['direction'] or 'None'}")
        print(f"Reason: {result['reason']}")

        if result['conditions']:
            print(f"\nConditions:")
            for k, v in result['conditions'].items():
                print(f"  {k}: {v}")

        if result['signal']:
            print(f"\nTrade Details:")
            print(f"  Entry: {result['entry_price']}")
            print(f"  SL: {result['sl']}")
            print(f"  Target: {result['target']}")
            print(f"  R:R = 1:{result['rr']}")
    else:
        print("❌ Could not fetch data for testing")
