"""
Example: using pandas inside run(). Keeps a short rolling window of mid prices per product,
computes a z-score of the current mid against its rolling mean, and leans against big moves
(buys when the mid is unusually low, sells when it is unusually high) with passive quotes.
A demonstration of the mechanics, not a recommended strategy.

Speed tips: keep a SHORT history (a plain list) and build a Series each tick, which is fast.
Rebuilding big DataFrames from your full history every tick gets slow: watch the
'tick time' line that run_local.py prints (the server limit is 100 ms per tick).

The history lives on `self`, so it is lost if the server restarts your process (e.g. after a
slow tick); the strategy just re-warms. Put anything you cannot afford to lose in trader_data.
"""
import math

import pandas as pd

from datamodel import Order, TradingState

WINDOW = 50       # ticks of mid history
ENTRY_Z = 1.5     # lean when |z| exceeds this
SIZE = 5


class Trader:
    def __init__(self):
        self.history = {}

    def run(self, state: TradingState):
        orders = {}
        for sym, depth in state.order_depths.items():
            bid, ask = depth.best_bid(), depth.best_ask()
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2
            h = self.history.setdefault(sym, [])
            h.append(mid)
            del h[:-WINDOW]                                # cap memory and per-tick work
            if len(h) < WINDOW:
                continue
            s = pd.Series(h)
            std = s.std()
            if not std or math.isnan(std):
                continue
            z = (mid - s.mean()) / std

            pos, lim = state.position.get(sym, 0), state.position_limits[sym]
            orders[sym] = []
            if z < -ENTRY_Z and pos < lim:                 # cheap vs recent history: bid at the touch + 1
                orders[sym].append(Order(sym, min(bid + 1, ask - 1), min(SIZE, lim - pos)))
            elif z > ENTRY_Z and pos > -lim:               # rich: offer at the touch - 1
                orders[sym].append(Order(sym, max(ask - 1, bid + 1), -min(SIZE, lim + pos)))
            elif abs(z) < 0.25 and pos != 0:               # back near the mean: flatten passively
                if pos > 0:
                    orders[sym].append(Order(sym, ask - 1 if ask - 1 > bid else ask, -min(SIZE, pos)))
                else:
                    orders[sym].append(Order(sym, bid + 1 if bid + 1 < ask else bid, min(SIZE, -pos)))
        return orders, 0, state.trader_data
