"""
Example: passive market maker around the book mid, with inventory skew.
A starting point, not a winning strategy: it knows nothing about any product beyond the visible book.

Shows: reading the book, quoting inside the spread, staying inside the position-limit rule,
and using trader_data (here: remembering the last mid for ticks when one side of the book is empty).
"""
import json
import math

from datamodel import Order, TradingState

SIZE = 5          # lots per quote
EDGE = 1          # quote at least this many cents away from our fair value
SKEW = 3.0        # cents of price shift per 100% of the position limit we hold


class Trader:
    def run(self, state: TradingState):
        last_mid = json.loads(state.trader_data) if state.trader_data else {}
        orders = {}
        for sym, depth in state.order_depths.items():
            bid, ask = depth.best_bid(), depth.best_ask()
            if bid is not None and ask is not None:
                last_mid[sym] = (bid + ask) / 2
            if sym not in last_mid or bid is None or ask is None:
                continue

            pos, lim = state.position.get(sym, 0), state.position_limits[sym]
            fair = last_mid[sym] - SKEW * pos / lim          # long -> we value it less, and vice versa

            # Improve the best quotes by one cent, but never quote through our own fair value.
            my_bid = min(bid + 1, math.floor(fair - EDGE))
            my_ask = max(ask - 1, math.ceil(fair + EDGE))

            # If buys (or sells) could breach the limit, ALL of this product's orders are rejected
            # for the tick, so never ask for more than the limit allows.
            buy_qty = min(SIZE, lim - pos)
            sell_qty = min(SIZE, lim + pos)
            orders[sym] = []
            if buy_qty > 0:
                orders[sym].append(Order(sym, my_bid, buy_qty))
            if sell_qty > 0:
                orders[sym].append(Order(sym, my_ask, -sell_qty))
        return orders, 0, json.dumps(last_mid)
