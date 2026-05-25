# ============================================================
# RISK MANAGER — INSTITUTIONAL VERSION
# New features:
# 1. OTM strike selection by ADX strength
# 2. Dual trigger SL (spot + premium ATR hybrid)
# 3. A+ setup validation
# 4. VIX regime filtering
# 5. Smart risk adjuster (Rs.500 -> min 20pt SL)
# 6. Skipped signal logging
# ============================================================

import json
import os
import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)


class RiskManager:

    def __init__(self, capital_settings: dict):
        self.total_capital = capital_settings['total']
        self.risk_per_trade = capital_settings.get('risk_per_trade', 1500)
        self.max_daily_loss = capital_settings.get('max_daily_loss', 3000)
        self.max_sl_per_day = capital_settings.get('max_sl_per_day', 2)
        self.premium_sl_pct = capital_settings.get('premium_sl_pct', 35)
        self.premium_sl_atr_mult = capital_settings.get('premium_sl_atr_mult', 2.5)
        self.premium_atr_period = capital_settings.get('premium_atr_period', 5)
        self.min_sl_points = capital_settings.get('min_sl_points', 20)
        self.lot_size = capital_settings.get('lot_size', 65)

        self.daily_pnl = 0.0
        self.daily_sl_count = 0
        self.daily_trades = 0
        self.daily_wins = 0
        self.trading_date = date.today()
        self.kill_switch_active = False
        self.trades_today = []

        os.makedirs('logs', exist_ok=True)

    # ── CAN TRADE ────────────────────────────────────────────
    def can_trade(self) -> tuple:
        self._check_new_day()
        if self.kill_switch_active:
            return False, 'Kill switch active'
        if self.daily_pnl <= -self.max_daily_loss:
            self._activate_kill_switch('Daily loss limit reached')
            return False, 'Daily loss limit Rs.' + str(self.max_daily_loss) + ' reached'
        if self.daily_sl_count >= self.max_sl_per_day:
            self._activate_kill_switch(str(self.max_sl_per_day) + ' SL hits today')
            return False, str(self.max_sl_per_day) + ' SL hits - trading stopped'
        return True, 'ok'

    # ── ADX + STRIKE SELECTION ───────────────────────────────
    def select_option_type(self, current_price: float,
                           direction: str, adx: float,
                           atr: float = 80) -> dict:
        """
        Select strike based on ADX strength.
        ADX 22-30  → ATM
        ADX 30-40  → +50 OTM (slight OTM)
        ADX 40-50  → ATM or +50 (stability priority)
        ADX > 50   → Should already be skipped upstream
        """
        atm_strike = round(current_price / 50) * 50

        if adx < 30:
            # ATM
            strike = atm_strike
            strike_type = 'ATM'
        elif adx < 40:
            # Slight OTM
            if direction == 'BUY':
                strike = atm_strike + 50
            else:
                strike = atm_strike - 50
            strike_type = 'OTM+50'
        else:
            # ADX 40-50: ATM for stability
            strike = atm_strike
            strike_type = 'ATM (stability)'

        suffix = 'CE' if direction == 'BUY' else 'PE'
        option_symbol = 'NIFTY' + str(strike) + suffix

        logger.info("Strike selected: %s (ADX=%.1f, type=%s)",
                    option_symbol, adx, strike_type)

        return {
            'type': strike_type,
            'strike': strike,
            'atm_strike': atm_strike,
            'option_symbol': option_symbol,
            'adx_used': round(adx, 1),
        }

    # ── VIX REGIME CHECK ─────────────────────────────────────
    def check_vix_regime(self, vix_data: dict, is_aplus: bool = False) -> tuple:
        """
        Check if VIX regime allows trading.
        Returns (can_trade, reason)
        """
        ratio = vix_data.get('ratio', 1.0)
        regime = vix_data.get('regime', 'NORMAL')

        if ratio > 1.5:
            return False, ('VIX ratio ' + str(ratio) +
                           ' > 1.5 (PANIC regime) - skip all trades')
        if ratio > 1.3 and not is_aplus:
            return False, ('VIX ratio ' + str(ratio) +
                           ' elevated (1.3-1.5) - A+ setups only')
        if ratio < 0.7:
            logger.info("VIX suppressed (ratio=%.2f) - theta decay zone", ratio)
            # Still trade but log the warning

        return True, regime

    # ── A+ SETUP VALIDATION ──────────────────────────────────
    def is_aplus_setup(self, adx: float, vix_ratio: float,
                       current_price: float, vwap: float,
                       ema200: float, ema200_prev: float,
                       supertrend_bullish: bool, direction: str,
                       has_exhaustion: bool,
                       current_time: str) -> tuple:
        """
        Validate A+ setup conditions.
        Returns (is_aplus, list_of_failed_conditions)
        """
        failed = []

        # 1. ADX 35-50
        if adx < 35:
            failed.append('ADX ' + str(round(adx,1)) + ' < 35')
        if adx >= 50:
            failed.append('ADX ' + str(round(adx,1)) + ' >= 50 (exhaustion)')

        # 2. VIX ratio < 1.5
        if vix_ratio >= 1.5:
            failed.append('VIX ratio ' + str(vix_ratio) + ' >= 1.5')

        # 3. VWAP distance <= 0.8%
        if vwap > 0:
            vwap_dist = abs(current_price - vwap) / vwap * 100
            if vwap_dist > 0.8:
                failed.append('VWAP distance ' + str(round(vwap_dist,2)) + '% > 0.8%')

        # 4. EMA200 slope aligned
        if ema200 > 0 and ema200_prev > 0:
            ema_slope_up = ema200 > ema200_prev
            if direction == 'BUY' and not ema_slope_up:
                failed.append('EMA200 slope not bullish')
            elif direction == 'SELL' and ema_slope_up:
                failed.append('EMA200 slope not bearish')

        # 5. Supertrend aligned
        if direction == 'BUY' and not supertrend_bullish:
            failed.append('Supertrend not bullish')
        elif direction == 'SELL' and supertrend_bullish:
            failed.append('Supertrend not bearish')

        # 6. No exhaustion candle
        if has_exhaustion:
            failed.append('Exhaustion candle in last 3 bars')

        # 7. Primary window only (9:25-11:30)
        if current_time < '09:25' or current_time > '11:30':
            failed.append('Outside primary window ' + current_time)

        is_aplus = len(failed) == 0
        return is_aplus, failed

    # ── SMART RISK ADJUSTER ──────────────────────────────────
    def adjust_trade_to_risk(self, entry: float, sl: float,
                              target: float, direction: str) -> dict:
        """
        Adjust SL to fit risk budget while preserving R:R.
        Skips trade if adjusted SL < min_sl_points.
        """
        original_sl_pts = abs(entry - sl)
        original_target_pts = abs(target - entry)
        original_risk_inr = round(original_sl_pts * self.lot_size)
        max_sl_pts = self.risk_per_trade / self.lot_size

        was_adjusted = False

        if original_risk_inr <= self.risk_per_trade:
            adjusted_sl = sl
            adjusted_sl_pts = original_sl_pts
            adjusted_target = target
            adjusted_target_pts = original_target_pts
            adjusted_risk_inr = original_risk_inr
        else:
            was_adjusted = True
            adjusted_sl_pts = max_sl_pts

            # Skip if adjusted SL is too tight (absolute minimum)
            if adjusted_sl_pts < self.min_sl_points:
                return {
                    'skip': True,
                    'skip_reason': 'sl_too_tight',
                    'reason': ('Adjusted SL ' + str(round(adjusted_sl_pts,1)) +
                               ' pts < minimum ' + str(self.min_sl_points) + ' pts. ' +
                               'Original risk Rs.' + str(original_risk_inr) +
                               ' too high for budget Rs.' + str(self.risk_per_trade)),
                    'original_sl_pts': round(original_sl_pts, 1),
                    'original_risk_inr': original_risk_inr,
                    'was_adjusted': True,
                }

            # Skip if SL compressed more than 50% from original
            # When strategy needs 49pts but we give 20pts = fake trade
            # Strategy's own logic is invalidated by extreme compression
            # Threshold: adjusted SL must be at least 50% of original
            compression_ratio = adjusted_sl_pts / original_sl_pts
            if compression_ratio < 0.50:
                compressed_pct = round((1 - compression_ratio) * 100)
                return {
                    'skip': True,
                    'skip_reason': 'extreme_compression',
                    'reason': ('SL compressed ' + str(compressed_pct) + '% from original. '
                               'Strategy needs ' + str(round(original_sl_pts,1)) + ' pts '
                               'but budget allows only ' + str(round(adjusted_sl_pts,1)) + ' pts. '
                               'Trade logic invalidated — skipping.'),
                    'original_sl_pts': round(original_sl_pts, 1),
                    'original_risk_inr': original_risk_inr,
                    'was_adjusted': True,
                    'compression_ratio': round(compression_ratio, 2),
                }

            if direction == 'BUY':
                adjusted_sl = round(entry - adjusted_sl_pts, 2)
            else:
                adjusted_sl = round(entry + adjusted_sl_pts, 2)

            original_rr = original_target_pts / original_sl_pts if original_sl_pts > 0 else 1.5
            adjusted_target_pts = adjusted_sl_pts * original_rr

            if direction == 'BUY':
                adjusted_target = round(entry + adjusted_target_pts, 2)
            else:
                adjusted_target = round(entry - adjusted_target_pts, 2)

            adjusted_risk_inr = round(adjusted_sl_pts * self.lot_size)

        rr = round(adjusted_target_pts / adjusted_sl_pts, 2) if adjusted_sl_pts > 0 else 1.5

        if was_adjusted:
            logger.info("Risk adjusted: SL %s->%s pts (Rs.%d->Rs.%d) R:R 1:%s",
                        round(original_sl_pts,1), round(adjusted_sl_pts,2),
                        original_risk_inr, adjusted_risk_inr, rr)

        return {
            'skip': False,
            'entry': entry,
            'sl': adjusted_sl,
            'target': adjusted_target,
            'direction': direction,
            'sl_points': round(adjusted_sl_pts, 2),
            'target_points': round(adjusted_target_pts, 2),
            'risk_inr': adjusted_risk_inr,
            'reward_inr': round(adjusted_target_pts * self.lot_size),
            'rr': rr,
            'was_adjusted': was_adjusted,
            'original_sl': sl,
            'original_target': target,
            'original_risk_inr': original_risk_inr,
        }

    # ── PREMIUM SL CALCULATION ───────────────────────────────
    def calculate_premium_sl(self, entry_premium: float,
                              premium_atr: float = None) -> dict:
        """
        Hybrid premium SL:
        = MIN(ATR-based SL, Hard % cap)

        ATR-based: entry - (2.5 × premium_ATR)
        Hard cap:  entry × (1 - 35%)

        Whichever gives higher SL (less loss) wins.
        """
        # Hard cap SL
        cap_sl = round(entry_premium * (1 - self.premium_sl_pct / 100), 2)

        if premium_atr and premium_atr > 0:
            # ATR-based SL
            atr_sl = round(entry_premium - (self.premium_sl_atr_mult * premium_atr), 2)
            # Take the higher value (less loss, more protection)
            premium_sl = max(cap_sl, atr_sl)
            method = 'ATR+Cap hybrid'
        else:
            premium_sl = cap_sl
            method = 'Cap only (no ATR data yet)'

        premium_sl = max(premium_sl, 5)  # Never below Rs.5

        return {
            'premium_sl': premium_sl,
            'entry_premium': entry_premium,
            'cap_sl': cap_sl,
            'atr_sl': atr_sl if premium_atr else None,
            'premium_atr': premium_atr,
            'method': method,
            'max_loss_per_unit': round(entry_premium - premium_sl, 2),
            'max_loss_total': round((entry_premium - premium_sl) * self.lot_size, 2),
        }

    # ── TRAILING SL ──────────────────────────────────────────
    def calculate_trailing_sl(self, entry: float, current_price: float,
                               original_sl: float, target: float,
                               direction: str, ema9: float,
                               beyond_target: bool = False) -> dict:
        """
        Trailing SL phases:
        50% target → Move to breakeven
        80% target → Trail 9 EMA
        Beyond target → Trail 9 EMA indefinitely
        """
        risk = abs(entry - original_sl)
        reward = abs(target - entry)

        if direction == 'BUY':
            pct = ((current_price - entry) / reward * 100) if reward > 0 else 0
        else:
            pct = ((entry - current_price) / reward * 100) if reward > 0 else 0
        pct = max(0, pct)

        if beyond_target:
            new_sl = (ema9 - risk * 0.05 if direction == 'BUY'
                      else ema9 + risk * 0.05)
            return {
                'new_sl': round(new_sl, 2), 'action': 'TRAIL_9EMA_EXTENDED',
                'pct_achieved': round(pct, 1),
                'message': 'Beyond target - trailing 9 EMA',
                'beyond_target': True,
            }

        if pct >= 80:
            new_sl = (ema9 - risk * 0.05 if direction == 'BUY'
                      else ema9 + risk * 0.05)
            action, msg = 'TRAIL_9EMA', '80% target - trailing 9 EMA'
        elif pct >= 50:
            new_sl = entry
            action, msg = 'MOVE_TO_BE', '50% target - SL to breakeven'
        else:
            new_sl = original_sl
            action, msg = 'HOLD', str(round(pct)) + '% toward target'

        return {
            'new_sl': round(new_sl, 2), 'action': action,
            'pct_achieved': round(pct, 1), 'message': msg,
            'beyond_target': False,
        }

    # ── POSITION SIZING ──────────────────────────────────────
    def calculate_position(self, entry: float, sl: float,
                           premium: float = None) -> dict:
        sl_pts = abs(entry - sl)
        if premium:
            lots = max(1, int(self.risk_per_trade / (premium * self.lot_size)))
            lots = min(lots, 1)
            actual_risk = round(premium * lots * self.lot_size * 0.35)
        else:
            risk_per_lot = sl_pts * self.lot_size
            lots = max(1, int(self.risk_per_trade / risk_per_lot)) if risk_per_lot > 0 else 1
            lots = min(lots, 1)
            actual_risk = round(sl_pts * lots * self.lot_size)

        logger.info("Position: %d lot(s), SL=%s pts, risk=Rs.%s",
                    lots, round(sl_pts, 1), actual_risk)
        return {
            'lots': lots, 'quantity': lots * self.lot_size,
            'risk_per_trade': actual_risk, 'sl_points': round(sl_pts, 1),
        }

    # ── SKIPPED SIGNAL LOGGER ────────────────────────────────
    def log_skipped_signal(self, strategy: str, direction: str,
                           reason: str, conditions: dict = None):
        """Log every skipped signal for review and filter tuning."""
        try:
            skipped_file = 'logs/skipped_signals.json'
            skipped = []
            if os.path.exists(skipped_file):
                with open(skipped_file, 'r') as f:
                    skipped = json.load(f)
            skipped.append({
                'datetime': datetime.now().isoformat(),
                'date': str(date.today()),
                'strategy': strategy,
                'direction': direction,
                'reason': reason,
                'conditions': conditions or {},
            })
            skipped = skipped[-200:]  # Keep last 200
            with open(skipped_file, 'w') as f:
                json.dump(skipped, f, indent=2, default=str)
        except Exception as e:
            logger.debug("Skipped signal log error: %s", e)

    # ── TRADE RECORDING ──────────────────────────────────────
    def record_trade_entry(self, trade: dict):
        self._check_new_day()
        self.daily_trades += 1
        trade['entry_time'] = datetime.now().isoformat()
        self.trades_today.append(trade)
        logger.info("Trade #%d entered: %s %s",
                    self.daily_trades, trade.get('direction'), trade.get('strategy'))

    def record_trade_exit(self, trade_id: str, result: str, pnl: float):
        self.daily_pnl += pnl
        if result == 'sl':
            self.daily_sl_count += 1
            logger.warning("SL hit #%d today. Daily P&L: Rs.%s",
                           self.daily_sl_count, round(self.daily_pnl))
            if self.daily_sl_count >= self.max_sl_per_day:
                self._activate_kill_switch(str(self.max_sl_per_day) + ' SL hits')
        elif result in ['target', 'extended_trail', 'squareoff']:
            self.daily_wins += 1
        if self.daily_pnl <= -self.max_daily_loss:
            self._activate_kill_switch('Daily loss limit reached')

    def get_daily_summary(self) -> dict:
        wr = (self.daily_wins / self.daily_trades * 100
              if self.daily_trades > 0 else 0)
        return {
            'date': str(self.trading_date),
            'total_trades': self.daily_trades,
            'wins': self.daily_wins,
            'sl_hits': self.daily_sl_count,
            'win_rate': round(wr, 1),
            'daily_pnl': round(self.daily_pnl, 2),
            'kill_switch': self.kill_switch_active,
        }

    def _activate_kill_switch(self, reason: str):
        if not self.kill_switch_active:
            self.kill_switch_active = True
            logger.warning("KILL SWITCH ACTIVATED: %s", reason)

    def _check_new_day(self):
        today = date.today()
        if today != self.trading_date:
            self.daily_pnl = 0.0
            self.daily_sl_count = 0
            self.daily_trades = 0
            self.daily_wins = 0
            self.trading_date = today
            self.kill_switch_active = False
            self.trades_today = []
            logger.info("New day - counters reset")

    def reset_kill_switch(self, confirm: bool = False):
        if confirm:
            self.kill_switch_active = False
            logger.warning("Kill switch manually reset")
