"""Heavier style: one growing DataFrame of all products (concat every tick), several rolling windows, ewm, correlation matrix."""
import pandas as pd
from datamodel import Order

class Trader:
    def __init__(self): self.df = pd.DataFrame()
    def run(self, state):
        row = {s: od.mid_price() for s, od in state.order_depths.items() if od.mid_price() is not None}
        self.df = pd.concat([self.df, pd.DataFrame([row])], ignore_index=True)
        df = self.df
        out = {}
        if len(df) < 60: return out, 0, ""
        feats = {}
        for w in (5, 20, 50):
            feats[w] = df.rolling(w).mean().iloc[-1]
        ew = df.ewm(span=30).mean().iloc[-1]
        corr = df.pct_change().tail(50).corr()
        vol = df.pct_change().rolling(20).std().iloc[-1]
        for sym, od in state.order_depths.items():
            sig = (feats[5][sym] - feats[50][sym]) / (vol[sym] * df[sym].iloc[-1] + 1e-9)
            pos = state.position.get(sym, 0)
            out[sym] = []
            if sig < -1 and pos < 40: out[sym].append(Order(sym, od.best_ask(), 1))
            if sig > 1 and pos > -40: out[sym].append(Order(sym, od.best_bid(), -1))
        return out, 0, ""
