"""Typical 'sensible' pandas use: keep a short history list, build a Series each tick, rolling z-score."""
import pandas as pd
from datamodel import Order

class Trader:
    def __init__(self): self.h = {}
    def run(self, state):
        out = {}
        for sym, od in state.order_depths.items():
            m = od.mid_price()
            if m is None: continue
            h = self.h.setdefault(sym, []); h.append(m); del h[:-200]
            if len(h) < 40: continue
            s = pd.Series(h)
            mu, sd = s.rolling(30).mean().iloc[-1], s.rolling(30).std().iloc[-1]
            z = (s.iloc[-1] - mu) / (sd + 1e-9)
            pos = state.position.get(sym, 0)
            out[sym] = []
            if z < -1.5 and pos < 40: out[sym].append(Order(sym, od.best_ask(), 1))
            if z > 1.5 and pos > -40: out[sym].append(Order(sym, od.best_bid(), -1))
        return out, 0, state.trader_data
