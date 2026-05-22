import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)


class LiveTrader:

    def __init__(self, kite, lot_size: int = 65):
        self.kite = kite
        self.lot_size = lot_size
        self._instruments = None
        self._instruments_date = None

    def get_funds(self) -> float:
        try:
            margins = self.kite.margins()
            available = margins['equity']['available']['live_balance']
            logger.info("Available funds: Rs.%s", available)
            return float(available)
        except Exception as e:
            logger.error("Funds fetch error: %s", e)
            return 0.0

    def _get_instruments(self):
        """Cache instruments for the day."""
        today = str(date.today())
        if self._instruments and self._instruments_date == today:
            return self._instruments
        try:
            self._instruments = self.kite.instruments('NFO')
            self._instruments_date = today
            logger.info("Instruments loaded: %d", len(self._instruments))
        except Exception as e:
            logger.error("Instruments fetch error: %s", e)
            self._instruments = []
        return self._instruments

    def get_option_symbol(self, strike: int, option_type: str,
                          expiry_type: str = 'current') -> str:
        """
        Get exact Zerodha tradingsymbol by looking up instruments.
        expiry_type: 'current' = nearest expiry, 'next' = next expiry
        """
        try:
            instruments = self._get_instruments()
            nifty_options = [
                i for i in instruments
                if i['name'] == 'NIFTY'
                and i['instrument_type'] == option_type
                and i['strike'] == float(strike)
            ]
            if not nifty_options:
                logger.error("No instruments found for NIFTY %d %s",
                             strike, option_type)
                return None

            # Sort by expiry
            nifty_options.sort(key=lambda x: x['expiry'])

            if expiry_type == 'current':
                chosen = nifty_options[0]
            else:
                chosen = nifty_options[1] if len(nifty_options) > 1 else nifty_options[0]

            symbol = chosen['tradingsymbol']
            logger.info("Symbol found: %s (expiry: %s)", symbol, chosen['expiry'])
            return symbol

        except Exception as e:
            logger.error("Symbol lookup error: %s", e)
            return None

    def get_live_option_price(self, symbol: str) -> float:
        """Get live LTP of an option from Zerodha."""
        try:
            instrument = f"NFO:{symbol}"
            quote = self.kite.ltp([instrument])
            ltp = quote[instrument]['last_price']
            return float(ltp)
        except Exception as e:
            logger.debug("LTP fetch error for %s: %s", symbol, e)
            return 0.0

    def place_entry_order(self, direction: str, strike: int,
                          option_type: str, lots: int = 1,
                          premium: float = None) -> dict:
        """Place entry order on Zerodha."""
        quantity = lots * self.lot_size

        # Get correct symbol from Zerodha instruments
        symbol = self.get_option_symbol(strike, option_type, 'current')
        if not symbol:
            return {'error': f'Symbol not found for {strike}{option_type}',
                    'status': 'failed'}

        try:
            logger.info("Placing order: BUY %s qty=%d", symbol, quantity)

            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NFO,
                tradingsymbol=symbol,
                transaction_type='BUY',
                quantity=quantity,
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=round(self.get_live_option_price(symbol) * 1.02, 1) or 1,
            )

            logger.info("Order placed: order_id=%s", order_id)
            fill_price = self._get_order_fill_price(order_id)

            return {
                'order_id': order_id,
                'symbol': symbol,
                'direction': direction,
                'strike': strike,
                'option_type': option_type,
                'quantity': quantity,
                'lots': lots,
                'fill_price': fill_price,
                'status': 'open',
                'entry_time': datetime.now().isoformat(),
            }

        except Exception as e:
            logger.error("Order placement error: %s", e)
            return {'error': str(e), 'status': 'failed'}

    def place_exit_order(self, symbol: str, quantity: int,
                         order_id: str = None) -> dict:
        """Place exit order."""
        try:
            logger.info("Placing exit: SELL %s qty=%d", symbol, quantity)

            exit_order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NFO,
                tradingsymbol=symbol,
                transaction_type='SELL',
                quantity=quantity,
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=0,
            )

            logger.info("Exit order placed: %s", exit_order_id)
            fill_price = self._get_order_fill_price(exit_order_id)

            return {
                'order_id': exit_order_id,
                'fill_price': fill_price,
                'status': 'closed',
                'exit_time': datetime.now().isoformat(),
            }

        except Exception as e:
            logger.error("Exit order error: %s", e)
            return {'error': str(e), 'status': 'failed'}

    def get_positions(self) -> list:
        try:
            positions = self.kite.positions()
            return positions.get('day', [])
        except Exception as e:
            logger.error("Positions fetch error: %s", e)
            return []

    def close_all_positions(self):
        """Emergency close all open intraday positions."""
        try:
            positions = self.get_positions()
            closed = 0
            for pos in positions:
                if pos['quantity'] != 0:
                    try:
                        tx = 'SELL' if pos['quantity'] > 0 else 'BUY'
                        self.kite.place_order(
                            variety=self.kite.VARIETY_REGULAR,
                            exchange=pos['exchange'],
                            tradingsymbol=pos['tradingsymbol'],
                            transaction_type=tx,
                            quantity=abs(pos['quantity']),
                            product=self.kite.PRODUCT_MIS,
                            order_type=self.kite.ORDER_TYPE_MARKET,
                price=0,
                        )
                        closed += 1
                    except Exception as e:
                        logger.error("Close error for %s: %s",
                                     pos['tradingsymbol'], e)
            logger.info("Emergency closed %d positions", closed)
            return closed
        except Exception as e:
            logger.error("Close all error: %s", e)
            return 0

    def _get_order_fill_price(self, order_id: str,
                               max_wait: int = 10) -> float:
        """Wait for order fill and return price."""
        import time
        for _ in range(max_wait):
            try:
                orders = self.kite.orders()
                for order in orders:
                    if str(order['order_id']) == str(order_id):
                        if order['status'] == 'COMPLETE':
                            return float(order['average_price'])
                        elif order['status'] == 'REJECTED':
                            logger.error("Order rejected: %s",
                                        order.get('status_message'))
                            return 0.0
            except Exception as e:
                logger.debug("Order status check: %s", e)
            time.sleep(1)
        return 0.0
