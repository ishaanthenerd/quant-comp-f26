"""
Example: using pandas inside run(). Same quoting idea as market_maker.py, but it only quotes
while recent volatility is low (a rolling std computed with pandas).

Speed tips: keep a SHORT history (a plain list) and build a Series each tick, which is fast.
Rebuilding big DataFrames from your full history every tick gets slow: watch the
'tick time' line that run_local.py prints (the server limit is 100 ms per tick).
"""
import math

import pandas as pd

from datamodel import Order

MAX_VOL = 0.9      # skip quoting when the rolling std of mid changes exceeds this


class Trader:
    def __init__(self):
        self.history = {}

    def run(self, state):
        orders = {}
        for sym, depth in state.order_depths.items():
            mid = depth.mid_price()
            if mid is None:
                continue
            h = self.history.setdefault(sym, [])
            h.append(mid)
            del h[:-200]                                   # cap memory and per-tick work
            vol = pd.Series(h).diff().rolling(20).std().iloc[-1] if len(h) > 21 else 0.0
            if vol > MAX_VOL:
                continue
            pos, lim = state.position.get(sym, 0), state.position_limits[sym]
            skew = -2.0 * pos / lim
            orders[sym] = []
            if lim - pos > 0:
                orders[sym].append(Order(sym, math.floor(mid + skew), min(4, lim - pos)))
            if lim + pos > 0:
                orders[sym].append(Order(sym, math.ceil(mid + skew), -min(4, lim + pos)))
        return orders, 0, state.trader_data
