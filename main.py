import sys, os, time, logging, argparse, json
import pandas as pd
from datetime import datetime, date

os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
sys.path.insert(0, '.')

from config.settings import (BROKER, CAPITAL, MODE, INDEX, MARKET,
    STRATEGY_PARAMS, STRATEGIES, TELEGRAM, DATA, LOGGING as LOG_CONFIG,
    ADX_STRIKE_RULES, VIX_RULES, APLUS_RULES)
from core.data_fetcher import DataFetcher
from core.indicators import Indicators
from core.risk_manager import RiskManager
from core.telegram_alerts import TelegramAlerter
from core.auto_squareoff import AutoSquareOff
from core.capital_tracker import CapitalTracker
from core.zerodha_login import ZerodhaLogin, do_daily_login
try:
    from github_sync import sync_after_trade, sync_after_skip
    GITHUB_SYNC = True
except Exception:
    GITHUB_SYNC = False
from core.live_trader import LiveTrader
from strategies.nifty_triple_sync import NiftyTripleSync
from strategies.fifteen_min_strategy import FifteenMinStrategy
from strategies.trap_trading import TrapTrading
from strategies.gap_fill_strategy import GapFillStrategy
from strategies.dby_strategy import DbyStrategy
from strategies.cb90_strategy import CB90Strategy

os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_CONFIG.get('level', 'INFO')),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

SL_LIMIT_BUFFER = 4.0  # 4-pt buffer between trigger and limit price


class TradingBot:

    def __init__(self, mode='paper'):
        self.mode = mode

        # Capital tracker
        self.capital_tracker = CapitalTracker(CAPITAL['total'])
        self.current_capital = self.capital_tracker.current_capital
        self.starting_capital = CAPITAL['total']

        logger.info("=" * 50)
        logger.info("TradeBot starting in %s mode", mode.upper())
        logger.info("Date: %s | Capital: Rs.%s", date.today(), self.current_capital)
        logger.info("=" * 50)

        settings = {"DATA": DATA, "INDEX": INDEX}
        self.fetcher = DataFetcher(settings)
        self.risk = RiskManager(CAPITAL)
        self.alerter = TelegramAlerter(TELEGRAM)
        self.ind = Indicators()
        self.squareoff = AutoSquareOff(broker=BROKER.get('name', 'zerodha'))

        # Holiday checker
        try:
            from core.holiday_checker import get_holiday_checker
            self.holiday_checker = get_holiday_checker()
            logger.info("Holidays: %d loaded", len(self.holiday_checker.holidays))
        except Exception as e:
            self.holiday_checker = None
            logger.warning("Holiday checker failed: %s", e)

        # Load strategies
        sp = STRATEGY_PARAMS
        self.strategies = {}
        if STRATEGIES.get('nifty_triple_sync'):
            self.strategies['NTS'] = NiftyTripleSync(sp['nifty_triple_sync'])
        if STRATEGIES.get('fifteen_min'):
            self.strategies['15MIN'] = FifteenMinStrategy(sp.get('fifteen_min', {}))
        if STRATEGIES.get('trap_trading'):
            self.strategies['TRAP'] = TrapTrading(sp.get('trap_trading', {}))
        if STRATEGIES.get('gap_fill'):
            self.strategies['GAP'] = GapFillStrategy(sp.get('gap_fill', {}))
        if STRATEGIES.get('dby_strategy'):
            self.strategies['DBY'] = DbyStrategy(sp.get('dby_strategy', {}))
        if STRATEGIES.get('cb90'):
            self.strategies['CB90'] = CB90Strategy(sp.get('cb90', {}))

        logger.info("Strategies loaded: %s", ', '.join(self.strategies.keys()))
        logger.info("Square-off: %s", self.squareoff.our_time)

        self.open_trades = {}
        self.pending_approvals = {}
        self.running = False
        self._vix_data = None
        self._vix_last_fetch = None

        # Live trader (only active in live mode)
        self.live_trader = None
        self.kite = None

        # Start Telegram command handler (listens for Start/Status/Capital)
        try:
            from core.telegram_commands import TelegramCommandHandler
            self.cmd_handler = TelegramCommandHandler(
                bot_token=TELEGRAM['bot_token'],
                chat_id=TELEGRAM['chat_id'],
                trading_bot=self)
            self.cmd_handler.start()
            logger.info("Telegram command handler started")
        except Exception as e:
            self.cmd_handler = None
            logger.warning("Telegram command handler failed: %s", e)

    # ── HELPERS ──────────────────────────────────────────────
    def _is_holiday(self):
        if self.holiday_checker:
            return self.holiday_checker.is_holiday(date.today())
        return False

    def _next_trading_day(self):
        if self.holiday_checker:
            return str(self.holiday_checker.get_next_trading_day())
        return 'next working day'

    def _cap_str(self):
        diff = round(self.current_capital - self.starting_capital, 2)
        return ('+Rs.' + str(diff)) if diff >= 0 else ('-Rs.' + str(abs(diff)))

    def _is_expiry_day(self):
        return date.today().weekday() == MARKET.get('expiry_day', 1)

    def _zerodha_position_qty(self, symbol):
        try:
            positions = self.kite.positions()
            for pos in positions.get("net", []):
                if pos["tradingsymbol"] == symbol:
                    return abs(pos["quantity"])
            return 0
        except Exception as e:
            logger.warning("Could not fetch Zerodha positions: %s", e)
            return -1

    def _zerodha_sl_fill_price(self, sl_order_id):
        try:
            orders = self.kite.orders()
            for o in orders:
                if str(o.get("order_id")) == str(sl_order_id):
                    if o.get("status") == "COMPLETE":
                        price = float(o.get("average_price", 0))
                        logger.info("SL order %s filled at Rs.%.2f", sl_order_id, price)
                        return price
            return 0
        except Exception as e:
            logger.warning("Could not fetch order history: %s", e)
        return 0

    def _get_vix(self):
        """Get VIX data, cached for 5 minutes."""
        try:
            cached = getattr(self, '_vix_cache', None)
            cached_time = getattr(self, '_vix_cache_time', None)
            if cached and cached_time and (datetime.now() - cached_time).seconds < 300:
                return cached
            vix_data = self.fetcher.get_vix()
            self._vix_cache = vix_data
            self._vix_cache_time = datetime.now()
            return vix_data
        except Exception as e:
            logger.debug("VIX fetch error: %s", e)
            return {'current': 15.0, 'ratio': 1.0, 'regime': 'NORMAL'}

    def run(self):
        self.running = True
        now = datetime.now()

        # ── WEEKEND CHECK (before login) ─────────────────────
        if date.today().weekday() >= 5:
            day_name = date.today().strftime('%A')
            logger.warning("Today is %s - no trading", day_name)
            self.alerter.send('Weekend - No Trading Today is ' + day_name + ' See you Monday!')
            return

        # ── HOLIDAY CHECK (before login) ──────────────────────
        if self.holiday_checker and self.holiday_checker.is_holiday():
            logger.warning("Today is a market holiday")
            self.alerter.send('Market Holiday Today - No Trading! Bot will start tomorrow.')
            return

        # ── LIVE MODE: Daily Zerodha Login ───────────────────
        if self.mode == 'live':
            logger.info("Live mode: initiating Zerodha login...")
            api_key = BROKER.get('api_key', '')
            api_secret = BROKER.get('api_secret', '')
            if not api_key or not api_secret:
                logger.error("API key/secret missing in settings.py!")
                self.alerter.send('Bot Error - API credentials missing in settings.py')
                return
            if not ZerodhaLogin.is_token_valid():
                login_handler = ZerodhaLogin(api_key, api_secret, TELEGRAM)
                login_handler.send_login_link()
                self.alerter.send('Daily login required! Check Telegram for the login link. You have 5 minutes.')
                success = login_handler.wait_for_login(timeout=300)
                if not success:
                    self.alerter.send('Login failed or timed out! Restart bot and try again.')
                    return
            login_handler = ZerodhaLogin(api_key, api_secret, TELEGRAM)
            self.kite = login_handler.get_kite_instance()
            if self.kite:
                self.fetcher.kite = self.kite  # enable Zerodha candle feed
                logger.info("DataFetcher: switched to Zerodha candle feed")
            if not self.kite:
                self.alerter.send("Kite connection failed! Restart bot.")
                return
            self.live_trader = LiveTrader(self.kite, lot_size=CAPITAL["lot_size"])
            funds = self.live_trader.get_funds()
            logger.info("Live mode ready. Funds: Rs.%s", funds)
            # Sync current capital from Zerodha but keep starting_capital fixed at Rs.50,000
            # starting_capital must never change — it is used for overall P&L calculation
            if funds > 0:
                self.current_capital = round(funds, 2)
                if self.capital_tracker:
                    self.capital_tracker.current_capital = self.current_capital
                    # Never overwrite starting_capital — keep original Rs.50,000
                    self.capital_tracker.save_capital(self.current_capital, reason='morning_zerodha_sync')
                logger.info("Capital synced from Zerodha: Rs.%s", self.current_capital)
            self.alerter.send('Zerodha Connected! Funds: Rs.' + str(funds) + ' Live trading starts at 9:15 AM')

        # ── WEEKEND CHECK (already handled above) ─────────────
        if date.today().weekday() >= 5:
            day_name = date.today().strftime('%A')
            logger.warning("Today is %s - no trading", day_name)
            self.alerter.send('Weekend - No Trading\nToday is ' + day_name)
            return

        # Holiday check
        if self.holiday_checker and self.holiday_checker.is_holiday():
            holiday_name = 'Market Holiday'
            logger.warning("Today is holiday: %s", holiday_name)
            self.alerter.send('Holiday - No Trading\n' + str(holiday_name))
            return

        # Startup message
        overall_pnl = self.current_capital - self.starting_capital
        overall = ('+Rs.' if overall_pnl >= 0 else '-Rs.') + str(abs(round(overall_pnl, 2)))
        active = ', '.join([k for k, v in STRATEGIES.items() if v])
        expiry_note = '\nExpiry day - no entries after 11:00 AM' if self._is_expiry_day() else ''
        now_str = datetime.now().strftime('%H:%M')
        mins_to_open = max(0, (9 * 60 + 15) - (datetime.now().hour * 60 + datetime.now().minute))
        msg = ('<b>TradeBot ' + ('Started' if mins_to_open > 0 else 'Ready') + '</b>\n'
               'Mode: ' + self.mode.upper() + '\n'
               'Date: ' + str(date.today()) + '\n'
               + ('Market opens in: ' + str(mins_to_open) + ' mins\n' if mins_to_open > 0 else 'Started: ' + now_str + '\n') +
               'Strategies: ' + active + '\n'
               'Capital: Rs.' + str(self.current_capital) + '\n'
               'Overall P&L: ' + overall +
               expiry_note + '\n'
               'Square-off: ' + self.squareoff.our_time + '\n'
               'DO NOT close CMD window!')
        self.alerter.send(msg)
        try:
            while self.running:
                self._tick()
                sleep_time = 5 if self.open_trades else 60
                time.sleep(sleep_time)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            if self.open_trades:
                self.alerter.send('Bot stopped manually! Check open positions in Zerodha!')
        finally:
            self.running = False


    def _tick(self):
        now = datetime.now()
        current_time = now.strftime('%H:%M')

        if self._is_holiday():
            return

        # Auto square-off
        should_sq, sq_reason = self.squareoff.should_squareoff()
        if should_sq and not self.squareoff.squared_off_today:
            logger.warning("Auto square-off: %s", sq_reason)
            self.alerter.send('<b>AUTO SQUARE-OFF</b>\n' + sq_reason)
            self._force_squareoff_all()
            self.squareoff.mark_squared_off()

        # Expiry day — exit all by 1 PM
        if (self._is_expiry_day() and
                current_time >= MARKET.get('expiry_exit_by', '13:00') and
                self.open_trades):
            logger.warning("Expiry day exit time reached")
            self.alerter.send('<b>Expiry Day Exit</b>\nClosing all positions by 1 PM')
            self._force_squareoff_all()

        # End of day
        if now.hour == 15 and now.minute >= 25:
            if self.running:
                self._end_of_day()
            return

        if not self.fetcher.is_market_open():
            if now.minute in [0, 30] and now.second < 65:
                m = ((9 * 60 + 15) - (now.hour * 60 + now.minute))
                if m > 0:
                    logger.info("Waiting for market. %d mins to open", m)
            return

        # Monitor open trades (dual trigger — every tick)
        if self.open_trades:
            self._monitor_trades()

        if self.risk.daily_trades >= 2:
            if now.minute % 30 == 0:
                logger.info("Max 2 trades today. Monitoring only.")
            return

        can_trade, reason = self.risk.can_trade()
        if not can_trade:
            if now.minute % 15 == 0:
                logger.info("Cannot trade: %s", reason)
            return

        # Expiry day — no fresh entries after 11 AM
        if (self._is_expiry_day() and
                current_time >= MARKET.get('expiry_no_entry_after', '11:00')):
            if now.minute % 15 == 0:
                logger.info("Expiry day - no entries after 11 AM")
            return

        self._scan_strategies()

    # ── SCAN STRATEGIES ──────────────────────────────────────
    def _scan_strategies(self):
        df_5m = self.fetcher.get_candles('NIFTY50', '5minute', 300)
        df_15m = self.fetcher.get_candles('NIFTY50', '15minute', 300)

        # Fetch VIX once per scan
        # Use Kite VIX if available (Connect plan), else fallback
        if self.kite and self.mode == 'live':
            vix_data = self.fetcher.get_vix_from_kite(self.kite)
        else:
            vix_data = self._get_vix()

        for name, strategy in self.strategies.items():
            try:
                df = df_15m if name == '15MIN' else df_5m
                if df is None or df.empty:
                    logger.warning("No data for %s", name)
                    continue
                result = strategy.check_signal(df)
                if result['signal']:
                    logger.info("SIGNAL [%s]: %s", name, result['reason'])
                    self._handle_signal(name, result, df_5m, vix_data)
                else:
                    if 'already fired' in result['reason']:
                        logger.info("%s: Day complete", name)
                    else:
                        logger.info("%s: %s", name, result['reason'])
            except Exception as e:
                logger.error("Strategy %s error: %s", name, e, exc_info=True)

    # ── HANDLE SIGNAL ────────────────────────────────────────
    def _handle_signal(self, strategy_name, signal, df, vix_data):
        signal_key = strategy_name + '_' + signal['direction'] + '_' + str(date.today())
        if signal_key in self.pending_approvals:
            return

        entry = signal['entry_price']
        direction = signal['direction']

        # ── ADX CHECK ────────────────────────────────────────
        try:
            adx = self.ind.adx_value(df)
        except Exception:
            adx = 25.0

        if adx < ADX_STRIKE_RULES['no_trade_below']:
            reason = 'ADX ' + str(round(adx,1)) + ' < ' + str(ADX_STRIKE_RULES['no_trade_below'])
            logger.info("[%s] SKIPPED: %s", strategy_name, reason)
            self.risk.log_skipped_signal(strategy_name, direction, reason,
                                          {'adx': round(adx,1)})
            if GITHUB_SYNC:
                try: sync_after_skip({'strategy': strategy_name, 'direction': direction, 'skip_reason': 'adx_too_low', 'reason': reason})
                except Exception: pass
            return

        if adx >= ADX_STRIKE_RULES['exhaustion_above']:
            reason = 'ADX ' + str(round(adx,1)) + ' >= 50 (exhaustion)'
            logger.info("[%s] SKIPPED: %s", strategy_name, reason)
            self.risk.log_skipped_signal(strategy_name, direction, reason,
                                          {'adx': round(adx,1)})
            return

        # ── VIX REGIME CHECK ─────────────────────────────────
        vix_ratio = vix_data.get('ratio', 1.0)
        vix_regime = vix_data.get('regime', 'NORMAL')

        vix_ok, vix_reason = self.risk.check_vix_regime(vix_data)

        if not vix_ok:
            logger.info("[%s] SKIPPED: VIX - %s", strategy_name, vix_reason)
            self.risk.log_skipped_signal(strategy_name, direction,
                'VIX: ' + vix_reason,
                {'vix': vix_data.get('current'), 'ratio': vix_ratio})
            return

        # ── SMART RISK ADJUSTER ──────────────────────────────
        adjusted = self.risk.adjust_trade_to_risk(
            entry=entry, sl=signal['sl'],
            target=signal['target'], direction=direction)

        if adjusted.get('skip'):
            logger.warning("[%s] SKIPPED: %s", strategy_name, adjusted['reason'])
            self.risk.log_skipped_signal(strategy_name, direction,
                adjusted['reason'],
                {'sl_pts': adjusted.get('original_sl_pts'),
                 'risk_inr': adjusted.get('original_risk_inr')})
            self.alerter.send(
                '<b>Signal Skipped - SL Too Tight</b>\n'
                'Strategy: ' + strategy_name + '\n'
                'Original SL: ' + str(round(adjusted['original_sl_pts'],1)) + ' pts\n'
                'Risk would be: Rs.' + str(adjusted['original_risk_inr']) + '\n'
                'Min SL needed: ' + str(self.risk.min_sl_points) + ' pts\n'
                'Waiting for better setup.'
            )
            return

        if adjusted['was_adjusted']:
            self.alerter.send(
                '<b>Trade Risk Adjusted</b>\n'
                'Strategy: ' + strategy_name + '\n'
                'Original SL: ' + str(signal['sl']) +
                ' (Rs.' + str(adjusted['original_risk_inr']) + ' risk)\n'
                'Adjusted SL: ' + str(adjusted['sl']) +
                ' (Rs.' + str(adjusted['risk_inr']) + ' risk)\n'
                'Adjusted Target: ' + str(adjusted['target']) + '\n'
                'R:R preserved: 1:' + str(adjusted['rr'])
            )

        # ── STRIKE SELECTION (ADX based) ─────────────────────
        option_info = self.risk.select_option_type(
            entry, direction, adx)

        # ── GET CORRECT SYMBOL FROM ZERODHA INSTRUMENTS ────
        if self.live_trader:
            correct_symbol = self.live_trader.get_option_symbol(
                option_info['strike'],
                'CE' if direction == 'BUY' else 'PE',
                'current')
            if correct_symbol:
                option_info['option_symbol'] = correct_symbol

        # ── REAL PREMIUM FROM KITE (Connect plan) ───────────
        real_premium = 0
        if self.live_trader:
            real_premium = self.live_trader.get_live_option_price(
                option_info['option_symbol'])
        if real_premium <= 0:
            real_premium = self.fetcher.get_option_premium(
                option_info['strike'],
                'CE' if direction == 'BUY' else 'PE')
        if real_premium <= 0:
            try:
                atr = self.ind.atr_value(df, 14)
                real_premium = max(80, min(300, round(atr * 0.4, 0)))
            except Exception:
                real_premium = 120
            except Exception:
                real_premium = 120

        logger.info("[%s] Premium: Rs.%s for %s",
                    strategy_name, real_premium, option_info['option_symbol'])

        # ── PREMIUM SL CALCULATION ───────────────────────────
        premium_key = str(option_info['strike']) + ('CE' if direction == 'BUY' else 'PE')
        premium_atr = self.fetcher.get_premium_atr(premium_key)
        prem_sl_data = self.risk.calculate_premium_sl(real_premium, premium_atr)

        position = self.risk.calculate_position(
            entry=adjusted['entry'], sl=adjusted['sl'], premium=real_premium)

        trade = {
            'id': signal_key,
            'strategy': strategy_name,
            'direction': direction,
            'entry': adjusted['entry'],
            'sl': adjusted['sl'],
            'original_sl': signal['sl'],
            'target': adjusted['target'],
            'original_target': signal['target'],
            'rr': adjusted['rr'],
            'option_symbol': option_info['option_symbol'],
            'strike': option_info['strike'],
            'strike_type': option_info['type'],
            'lots': position['lots'],
            'entry_premium': real_premium,
            'premium_sl': prem_sl_data['premium_sl'],
            'premium_sl_data': prem_sl_data,
            'risk_inr': adjusted['risk_inr'],
            'reward_inr': adjusted['reward_inr'],
            'was_adjusted': adjusted['was_adjusted'],
            'adx_at_entry': round(adx, 1),
            'vix_at_entry': vix_data.get('current', 0),
            'vix_ratio_at_entry': vix_ratio,
            'vix_regime': vix_regime,
            'signal_time': datetime.now().isoformat(),
            'reason': signal['reason'],
            'be_alerted': False,
            'beyond_target': False,
        }

        self.pending_approvals[signal_key] = trade

        if self.mode == 'paper':
            logger.info("PAPER TRADE: Auto-approving [%s]", strategy_name)
            self._execute_trade(trade)
        else:
            self._execute_trade(trade)

    # ── EXECUTE TRADE ────────────────────────────────────────
    def _execute_trade(self, trade):
        trade['status'] = 'open'
        trade['mode'] = self.mode
        # Prevent duplicate direction trades
        for existing in self.open_trades.values():
            if existing['direction'] == trade['direction']:
                logger.warning("[%s] SKIPPED: Already have open %s trade (%s)",
                               trade['strategy'], trade['direction'],
                               existing['strategy'])
                msg = ('Signal Skipped - Duplicate Direction' +
                    ' Strategy: ' + trade['strategy'] +
                    ' Already have open ' + trade['direction'] + ' trade from ' +
                    existing['strategy'])
                self.alerter.send(msg)
                return
        # Place real order in live mode FIRST — only register trade after confirmed fill
        if self.mode == 'live' and self.live_trader:
            option_type = 'CE' if trade['direction'] == 'BUY' else 'PE'
            order_result = self.live_trader.place_entry_order(
                direction=trade['direction'],
                strike=trade['strike'],
                option_type=option_type,
                lots=trade['lots']
            )
            if order_result.get('status') == 'failed':
                logger.error('Order placement failed: %s', order_result.get('error'))
                self.alerter.send(
                    '<b>ORDER FAILED - Trade NOT executed</b>\n'
                    'Strategy: ' + trade['strategy'] + '\n'
                    'Reason: ' + str(order_result.get('error')) + '\n'
                    'No P&L impact. Bot continues watching.')
                return  # ← exit without registering trade anywhere

            trade['order_id'] = order_result.get('order_id')
            trade['option_symbol'] = order_result.get('symbol', trade['option_symbol'])
            fill_price = order_result.get('fill_price', 0)

            if fill_price <= 0:
                logger.error('Order fill price is 0 - order may not have filled!')
                self.alerter.send(
                    '<b>ORDER WARNING - Fill price is 0</b>\n'
                    'Strategy: ' + trade['strategy'] + '\n'
                    'Order ID: ' + str(order_result.get('order_id', '?')) + '\n'
                    'Check Zerodha app immediately!\n'
                    'Trade NOT counted until confirmed.')
                return  # ← exit without registering trade

            trade['entry_premium'] = fill_price
            logger.info('Live order confirmed: %s at Rs.%s',
                        trade['option_symbol'], fill_price)

        # ── Register trade ONLY after confirmed fill ──────────
        # Paper mode: always registers
        # Live mode: only reaches here if fill_price > 0
        self.open_trades[trade['id']] = trade
        self.risk.record_trade_entry(trade)
        # NOTE: Do NOT log to journal at entry — log only at exit
        # This prevents ghost entries with no exit data in trades.json

        # FIX: Single SL order at premium_sl price with 4-point buffer
        if self.mode == 'live' and self.live_trader and trade.get('option_symbol') and trade.get('entry_premium', 0) > 0 and not trade.get('sl_order_placed'):
            fill_price = trade.get('entry_premium', 0)
            if fill_price > 0:
                sl_trigger = round(float(trade['premium_sl']), 1)
                sl_limit   = max(round(sl_trigger - SL_LIMIT_BUFFER, 1), 1.0)
                sl_result = self.live_trader.place_sl_order(
                    symbol=trade['option_symbol'],
                    quantity=trade['lots'] * CAPITAL['lot_size'],
                    trigger_price=sl_trigger,
                    price=sl_limit)
                if sl_result.get('status') == 'placed':
                    trade['sl_order_id']     = sl_result['order_id']
                    trade['sl_order_placed'] = True
                    logger.info('SL order placed: id=%s trigger=%.1f limit=%.1f',
                                sl_result['order_id'], sl_trigger, sl_limit)
                    self.alerter.send(
                        '<b>SL Order Placed</b>\n'
                        'Trigger: Rs.' + str(sl_trigger) + ' (Premium SL)\n'
                        'Limit: Rs.'   + str(sl_limit)   + ' (4-pt buffer)')
                else:
                    logger.warning('SL order failed: %s', sl_result.get('error'))
                    self.alerter.send('WARNING: SL order failed! Monitor manually.\nError: ' + str(sl_result.get('error', '')))


        prem_sl = trade.get('premium_sl', 0)
        prem_sl_data = trade.get('premium_sl_data', {})

        msg = ('<b>TRADE ENTERED [' + self.mode.upper() + ']</b>\n'
               'Strategy: ' + trade['strategy'] + '\n'
               'Direction: ' + trade['direction'] +
               ' (' + trade['option_symbol'] + ')\n'
               'Strike type: ' + trade.get('strike_type', 'ATM') +
               ' (ADX=' + str(trade.get('adx_at_entry', '?')) + ')\n'
               'Index Entry: ' + str(trade['entry']) + '\n'
               'Option Premium: Rs.' + str(trade['entry_premium']) + '/unit (LIVE)\n'
               'Total Premium: Rs.' + str(round(trade['entry_premium'] * trade['lots'] * CAPITAL['lot_size'], 2)) + '\n'
               'Spot SL: ' + str(trade['sl']) + '\n'
               'Premium SL: Rs.' + str(prem_sl) + '/unit\n'
               '  (' + prem_sl_data.get('method', '') + ')\n'
               'Target: ' + str(trade['target']) + '\n'
               'R:R = 1:' + str(trade['rr']) + '\n'
               'Risk: Rs.' + str(trade['risk_inr']) + '\n'
               'VIX: ' + str(trade.get('vix_at_entry', '?')) +
               ' (ratio=' + str(trade.get('vix_ratio_at_entry', '?')) + ')\n'
               'Capital: Rs.' + str(self.current_capital))
        self.alerter.send(msg)

        logger.info("[%s] ENTRY: %s %s | Entry:%s SL:%s PremSL:Rs.%s Target:%s",
                    trade['strategy'], trade['direction'],
                    trade['option_symbol'], trade['entry'],
                    trade['sl'], prem_sl, trade['target'])

    # ── MONITOR TRADES (DUAL TRIGGER) ───────────────────────
    # ── MONITOR TRADES (DUAL TRIGGER, 5-SEC WHEN OPEN) ──────
    def _monitor_trades(self):
        current_price = self.fetcher.get_live_price('NIFTY50')
        if current_price == 0:
            return

        for trade_id, trade in list(self.open_trades.items()):
            d = trade['direction']
            sl = trade['sl']
            target = trade['target']
            entry_premium = trade.get('entry_premium', 120)
            premium_sl = trade.get('premium_sl', entry_premium * 0.65)

            # ── SPOT SL HIT (touch basis, checked every 5 sec) ──
            spot_sl_hit = ((d == 'BUY' and current_price <= sl) or
                           (d == 'SELL' and current_price >= sl))

            # ── PREMIUM SL HIT ──────────────────────────────────
            premium_key = str(trade['strike']) + ('CE' if d == 'BUY' else 'PE')
            # Use Kite LTP (Connect plan) for real premium
            option_symbol = trade.get('option_symbol', '')
            if option_symbol and self.live_trader:
                current_premium = self.live_trader.get_live_option_price(option_symbol)
            else:
                current_premium = self.fetcher.get_option_premium(
                    trade['strike'], 'CE' if d == 'BUY' else 'PE')
            if current_premium > 0:
                self.fetcher._update_premium_cache(premium_key, current_premium)
            premium_sl_hit = (current_premium > 0 and current_premium <= premium_sl)

            # ── DUAL TRIGGER EXIT ───────────────────────────────
            if spot_sl_hit or premium_sl_hit:
                trigger = 'spot_sl' if spot_sl_hit else 'premium_sl'
                logger.info("[%s] EXIT triggered by %s", trade['strategy'], trigger)
                trade['exit_trigger'] = trigger
                trade['current_premium_at_exit'] = current_premium
                self._close_trade(trade_id, current_price, 'sl')
                continue

            # ── TARGET HIT → START TRAILING 9 EMA ─────────────
            target_hit = ((d == 'BUY' and current_price >= target) or
                          (d == 'SELL' and current_price <= target))
            if target_hit and not trade.get('beyond_target'):
                trade['beyond_target'] = True
                trade['sl'] = trade['entry']  # move SL to entry first
                logger.info("[%s] TARGET hit at %s - now trailing 9 EMA",
                            trade['strategy'], round(current_price))
                self.alerter.send(
                    '<b>Target Hit! Trailing 9 EMA</b>\n'
                    'Strategy: ' + trade['strategy'] + '\n'
                    'Target reached: ' + str(round(current_price)) + '\n'
                    'SL moved to entry: ' + str(trade['entry']) + '\n'
                    'Will exit when price crosses 9 EMA. Watching every 5 sec...'
                )
                # Don't continue — fall through to trailing logic below

            # ── TRAILING SL ──────────────────────────────────────
            # Phase 1 (before target): 50% → BE, 80% → trail 9 EMA
            # Phase 2 (beyond target): trail 9 EMA indefinitely
            # Exit immediately when price crosses 9 EMA in Phase 2
            try:
                df_trail = self.fetcher.get_candles('NIFTY50', '5minute', 20)
                if not df_trail.empty:
                    ema9 = self.ind.ema_value(df_trail, 9)
                    beyond = trade.get('beyond_target', False)
                    t = self.risk.calculate_trailing_sl(
                        trade['entry'], current_price, trade['sl'],
                        target, d, ema9, beyond_target=beyond)

                    if beyond:
                        # Check if price has crossed 9 EMA — exit immediately
                        ema_crossed = ((d == 'BUY' and current_price < ema9) or
                                       (d == 'SELL' and current_price > ema9))
                        if ema_crossed:
                            logger.info("[%s] 9 EMA crossed after target — exiting",
                                        trade['strategy'])
                            trade['exit_trigger'] = 'ema9_trail'
                            trade['current_premium_at_exit'] = current_premium
                            self._close_trade(trade_id, current_price, 'extended_trail')
                            continue
                        # Update trailing SL
                        if trade['sl'] != t['new_sl']:
                            trade['sl'] = t['new_sl']
                            logger.info("Trail SL updated: %s (beyond target)",
                                        t['new_sl'])
                    else:
                        if (t['action'] in ['MOVE_TO_BE', 'TRAIL_9EMA']
                                and trade['sl'] != t['new_sl']):
                            trade['sl'] = t['new_sl']
                            logger.info("SL updated: %s (%s)", t['new_sl'], t['action'])
                            if t['action'] == 'MOVE_TO_BE' and not trade.get('be_alerted'):
                                self.alerter.send(
                                    '<b>SL Moved to Breakeven</b>\n'
                                    'Strategy: ' + trade['strategy'] + '\n'
                                    'SL = Entry: ' + str(trade['entry']) + '\n'
                                    'Cannot lose on this trade now!\n'
                                    'Nifty: ' + str(round(current_price))
                                )
                                trade['be_alerted'] = True
            except Exception as e:
                logger.error("Trail SL error: %s", e)


    def _force_squareoff_all(self):
        if not self.open_trades:
            logger.info("No open trades to square off")
            return
        p = self.fetcher.get_live_price('NIFTY50')
        count = len(self.open_trades)
        for tid in list(self.open_trades.keys()):
            t = self.open_trades[tid]
            ep = p if p > 0 else t['entry']
            self._close_trade(tid, ep, 'squareoff')
        self.alerter.send(
            '<b>SQUARE-OFF COMPLETE</b>\n' +
            str(count) + ' position(s) closed\n'
            'Capital: Rs.' + str(round(self.current_capital))
        )

    # ── CLOSE TRADE ──────────────────────────────────────────
    def _close_trade(self, trade_id, exit_price, result):
        trade = self.open_trades.pop(trade_id, None)
        if not trade:
            return

        d = trade['direction']
        points = (exit_price - trade['entry'] if d == 'BUY'
                  else trade['entry'] - exit_price)
        lot_size = CAPITAL.get('lot_size', 65)
        entry_prem_calc = trade.get('entry_premium', 0)
        exit_prem_calc = trade.get('current_premium_at_exit', 0)
        if entry_prem_calc > 0 and exit_prem_calc > 0:
            pnl = round((exit_prem_calc - entry_prem_calc) * trade.get('lots', 1) * lot_size, 2)
        else:
            pnl = round(points * trade.get('lots', 1) * lot_size * 0.05, 2)

        self.current_capital = round(self.current_capital + pnl, 2)
        if self.capital_tracker:
            self.capital_tracker.update(self.current_capital)
            self.capital_tracker.save_capital(self.current_capital, reason=result)

        entry_prem = trade.get('entry_premium', 120)
        exit_prem = trade.get('current_premium_at_exit', 0)
        if exit_prem <= 0:
            if result in ['target', 'extended_trail']:
                exit_prem = round(entry_prem * (1 + abs(points)/trade['entry']*10), 0)
            elif result == 'sl':
                exit_prem = round(entry_prem * 0.35, 0)
            else:
                exit_prem = round(entry_prem * (1 + points/trade['entry']*8), 0)
        exit_prem = max(5, exit_prem)

        # Map result to website-compatible values
        result_display = result
        if result in ['extended_trail', 'squareoff', 'manual_exit', 'zerodha_autosquareoff']:
            result_display = 'target'

        trade.update({
            'exit': exit_price,
            'result': result_display,
            'result_raw': result,
            'points': round(points, 2),
            'pnl': pnl,
            'exit_premium': exit_prem,
            'exit_time': datetime.now().isoformat(),
            'capital_after': self.current_capital,
            # Fields required by tradelog website
            'date': str(date.today()),
            'time': datetime.now().strftime('%H:%M'),
            'entry_price': trade.get('entry'),
            'exit_price': exit_price,
            'vix_ratio': trade.get('vix_ratio_at_entry', ''),
        })
        self.risk.record_trade_exit(trade_id, result, pnl)

        # NOTE: _log_to_journal and sync_after_trade are called AFTER
        # the live fill correction below, so the journal always reflects
        # the actual Zerodha fill price, not the monitoring-time LTP.
        # For paper mode, we log here immediately since there is no fill.
        if self.mode != 'live':
            self._log_to_journal(trade)
            if GITHUB_SYNC:
                try:
                    sync_after_trade(trade)
                except Exception as e:
                    logger.debug("GitHub sync error: %s", e)

        # Live mode: check if Zerodha already closed position before placing exit
        already_closed = False
        if self.mode == 'live' and self.live_trader and trade.get('option_symbol'):
            qty = self._zerodha_position_qty(trade['option_symbol'])
            if qty == 0 and trade.get('sl_order_placed'):
                already_closed = True
                logger.info('[%s] Already closed by Zerodha SL order', trade['strategy'])
                if trade.get('sl_order_id'):
                    fill = self._zerodha_sl_fill_price(trade['sl_order_id'])
                    if fill > 0:
                        trade['current_premium_at_exit'] = fill
                        logger.info('Using Zerodha SL fill price: Rs.%.2f', fill)
                self.alerter.send(
                    '<b>Position closed by Zerodha SL order</b>\n'
                    'Strategy: ' + trade['strategy'] + '\n'
                    'Skipping duplicate exit order.')

        if self.mode == 'live' and self.live_trader and not already_closed:
            try:
                sl_order_id = trade.get('sl_order_id')
                if sl_order_id and trade.get('sl_order_placed'):
                    self.live_trader.cancel_order(sl_order_id)
                    logger.info('SL order cancelled: %s', sl_order_id)
                exit_result = self.live_trader.place_exit_order(
                    symbol=trade.get('option_symbol', ''),
                    quantity=trade.get('lots', 1) * CAPITAL['lot_size']
                )
                if exit_result.get('fill_price', 0) > 0:
                    actual_fill = exit_result['fill_price']
                    trade['exit_premium'] = actual_fill
                    trade['current_premium_at_exit'] = actual_fill
                    logger.info('Live exit placed at Rs.%s', actual_fill)

                    # ── RECALCULATE P&L with actual fill price ──────
                    # Previous P&L used monitoring price — now correct it
                    entry_prem = trade.get('entry_premium', 0)
                    lot_size   = CAPITAL.get('lot_size', 65)
                    if entry_prem > 0:
                        real_pnl = round((actual_fill - entry_prem) * trade.get('lots', 1) * lot_size, 2)
                        old_pnl  = pnl
                        pnl      = real_pnl
                        # Update capital with corrected P&L
                        self.current_capital = round(self.current_capital - old_pnl + real_pnl, 2)
                        if self.capital_tracker:
                            self.capital_tracker.update(self.current_capital)
                            self.capital_tracker.save_capital(self.current_capital, reason=result + '_corrected')
                        trade['pnl']           = real_pnl
                        trade['capital_after'] = self.current_capital
                        logger.info('P&L corrected: Rs.%s → Rs.%s (fill=%.2f)',
                                    old_pnl, real_pnl, actual_fill)
            except Exception as e:
                logger.error('Live exit order error: %s', e)
                self.alerter.send(
                    '<b>EXIT ORDER FAILED!</b>\n'
                    'Strategy: ' + trade.get('strategy', '?') + '\n'
                    'Symbol: ' + trade.get('option_symbol', '?') + '\n'
                    'Error: ' + str(e) + '\n'
                    '<b>CLOSE MANUALLY IN ZERODHA NOW!</b>')

        pnl_sign = '+' if pnl >= 0 else ''
        cap_s = self._cap_str()
        trigger = trade.get('exit_trigger', 'spot')

        if result == 'sl':
            msg = ('STOP LOSS HIT\n'
                   'Strategy: ' + trade['strategy'] + '\n'
                   'Exit trigger: ' + trigger + '\n'
                   'Entry: ' + str(trade['entry']) + ' | Exit: ' + str(exit_price) + '\n'
                   'Entry Premium: Rs.' + str(entry_prem) + '/unit\n'
                   'Exit Premium: Rs.' + str(exit_prem) + '/unit\n'
                   'Points Lost: ' + str(round(points,1)) + '\n'
                   'Loss: Rs.' + str(abs(pnl)) + '\n'
                   'SL hits today: ' + str(self.risk.daily_sl_count) + '/2\n'
                   'Capital: Rs.' + str(self.current_capital) +
                   ' (' + cap_s + ' overall)\n'
                   'Stay disciplined.')
        elif result == 'extended_trail':
            msg = ('EXTENDED TRAIL EXIT - EMA Broken\n'
                   'Strategy: ' + trade['strategy'] + '\n'
                   'Entry: ' + str(trade['entry']) + '\n'
                   'Original Target: ' + str(trade['original_target']) + '\n'
                   'Exit: ' + str(exit_price) + '\n'
                   'Entry Premium: Rs.' + str(entry_prem) + '/unit\n'
                   'Exit Premium: Rs.' + str(exit_prem) + '/unit\n'
                   'Total Points: ' + pnl_sign + str(round(points,1)) + '\n'
                   'Total Profit: Rs.' + pnl_sign + str(pnl) + '\n'
                   'Capital: Rs.' + str(self.current_capital) +
                   ' (' + cap_s + ' overall)\n'
                   'Let the winner run!')
        elif result == 'manual_stop':
            msg = ('TRADE CLOSED - Bot Stopped\n'
                   'Exit: ' + str(exit_price) + '\n'
                   'P&L: Rs.' + pnl_sign + str(pnl) + '\n'
                   'Capital: Rs.' + str(self.current_capital))
        else:
            msg = ('SQUARE-OFF / TARGET\n'
                   'Strategy: ' + trade['strategy'] + '\n'
                   'Entry: ' + str(trade['entry']) + ' | Exit: ' + str(exit_price) + '\n'
                   'Entry Premium: Rs.' + str(entry_prem) + '/unit\n'
                   'Exit Premium: Rs.' + str(exit_prem) + '/unit\n'
                   'Points: ' + pnl_sign + str(round(points,1)) + '\n'
                   'P&L: Rs.' + pnl_sign + str(pnl) + '\n'
                   'Capital: Rs.' + str(self.current_capital) +
                   ' (' + cap_s + ' overall)')
        self.alerter.send(msg)

        if self.risk.kill_switch_active:
            self.alerter.send(
                '<b>KILL SWITCH ACTIVATED</b>\n'
                'Reason: ' + str(self.risk.daily_sl_count) + ' SL hits\n'
                'No more trades today.\nReview journal tonight.'
            )
        logger.info("[%s] CLOSED: %s | Pts:%+.1f | PnL:Rs.%+.0f | Cap:Rs.%s",
                    trade.get('strategy',''), result.upper(),
                    points, pnl, self.current_capital)

    # ── JOURNAL ──────────────────────────────────────────────
    def _log_to_journal(self, trade):
        try:
            log_file = 'logs/trades.json'
            trades = []
            if os.path.exists(log_file):
                with open(log_file, 'r') as f:
                    trades = json.load(f)
            trades.append(trade)
            with open(log_file, 'w') as f:
                json.dump(trades, f, indent=2, default=str)
        except Exception as e:
            logger.error("Journal error: %s", e)

    # ── STOPPED ALERT ────────────────────────────────────────
    def _send_stopped_alert(self, reason='Unknown'):
        summary = self.risk.get_daily_summary()
        cap_s = self._cap_str()
        self.alerter.send(
            '<b>TradeBot Stopped</b>\n'
            'Reason: ' + reason + '\n'
            'Time: ' + datetime.now().strftime('%H:%M:%S') + '\n'
            'Trades: ' + str(summary['total_trades']) + '\n'
            'Wins: ' + str(summary['wins']) +
            ' | SL: ' + str(summary['sl_hits']) + '\n'
            'Today P&L: Rs.' + str(summary['daily_pnl']) + '\n'
            'Capital: Rs.' + str(self.current_capital) +
            ' (' + cap_s + ' overall)\n'
            'See you tomorrow!'
        )

    # ── END OF DAY ───────────────────────────────────────────
    def _end_of_day(self):
        if self.open_trades:
            self._force_squareoff_all()
        self.running = False
        summary = self.risk.get_daily_summary()
        cap_s = self._cap_str()

        if self.capital_tracker:
            self.capital_tracker.save_capital(
                self.current_capital, reason='end_of_day')

        logger.info("=" * 50)
        logger.info("END OF DAY | Trades:%d Wins:%d SL:%d WR:%s%% P&L:Rs.%s",
                    summary['total_trades'], summary['wins'],
                    summary['sl_hits'], summary['win_rate'],
                    summary['daily_pnl'])
        logger.info("=" * 50)

        self.alerter.send(
            '<b>Daily Summary</b>\n'
            'Date: ' + str(date.today()) + '\n'
            'Trades: ' + str(summary['total_trades']) + '\n'
            'Wins: ' + str(summary['wins']) +
            ' | SL: ' + str(summary['sl_hits']) + '\n'
            'Win Rate: ' + str(summary['win_rate']) + '%\n'
            'Today P&L: Rs.' + str(summary['daily_pnl']) + '\n'
            'Capital: Rs.' + str(self.current_capital) + '\n'
            'Overall: ' + cap_s
        )
        self._send_stopped_alert('End of trading day (3:25 PM)')

        # Auto sync to GitHub journal at end of day
        try:
            from github_sync import sync_all
            sync_all(reason='end_of_day_' + str(date.today()))
            logger.info("Journal synced to GitHub")
        except Exception as e:
            logger.warning("Journal sync failed: %s", e)

        for s in self.strategies.values():
            if hasattr(s, 'reset_daily'):
                s.reset_daily()


# ── ENTRY POINT ──────────────────────────────────────────────
if __name__ == '__main__':
    import pandas as pd
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()

    if args.test:
        print('\nRunning system test...\n')
        settings = {'DATA': DATA, 'INDEX': INDEX}
        fetcher = DataFetcher(settings)
        sq = AutoSquareOff(broker=BROKER.get('name', 'zerodha'))
        ct = CapitalTracker(CAPITAL['total'])
        print('Capital loaded: Rs.' + str(ct.current_capital))
        print('Strategies: ' + str([k for k,v in STRATEGIES.items() if v]))
        print('Square-off: ' + sq.our_time)
        try:
            from core.holiday_checker import get_holiday_checker
            hc = get_holiday_checker()
            print('Holidays: ' + str(len(hc.holidays)) + ' loaded')
            print('Today is holiday: ' + str(hc.is_holiday()))
        except Exception as e:
            print('Holiday checker: ' + str(e))
        print('Weekend: ' + str(date.today().weekday() >= 5))
        vix = fetcher.get_vix()
        print('VIX: ' + str(vix.get('current')) +
              ' | Ratio: ' + str(vix.get('ratio')) +
              ' | Regime: ' + str(vix.get('regime')))
        df = fetcher.get_candles('NIFTY50', '5minute', 50)
        if not df.empty:
            print('Data: ' + str(len(df)) + ' candles | Latest: ' +
                  str(round(df['close'].iloc[-1], 2)))
        ind = Indicators()
        if not df.empty and len(df) >= 30:
            adx = ind.adx_value(df)
            print('ADX: ' + str(round(adx,1)) +
                  (' Strong' if adx > 25 else ' Weak'))
        print('\nSystem test complete!')
        sys.exit(0)

    mode = 'live' if args.live else 'paper'
    if mode == 'live':
        if False:
            sys.exit(0)
    bot = TradingBot(mode=mode)
    bot.run()
