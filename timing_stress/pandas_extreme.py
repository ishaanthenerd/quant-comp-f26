"""Worst realistic style: rebuild a DataFrame from FULL history every tick and compute many features."""
import pandas as pd, numpy as np
from datamodel import Order

class Trader:
    def __init__(self): self.hist = []
    def run(self, state):
        self.hist.append({s: od.mid_price() for s, od in state.order_depths.items()})
        out = {}
        if len(self.hist) < 60: return out, 0, ""
        df = pd.DataFrame(self.hist)
        f = pd.concat({f"ma{w}": df.rolling(w).mean() for w in (3, 5, 10, 20, 40, 80)}, axis=1)
        f = f.join(df.rolling(20).std().add_prefix("sd_")) if False else f
        rk = df.rank(axis=1)
        cov = df.pct_change().cov()
        ranked = df.tail(300).sort_values(df.columns[0])
        for sym in state.order_depths:
            sig = f[("ma5", sym)].iloc[-1] - f[("ma80", sym)].iloc[-1]
            pos = state.position.get(sym, 0)
            out[sym] = []
            if sig < -2 and pos < 40: out[sym].append(Order(sym, state.order_depths[sym].best_ask(), 1))
            if sig > 2 and pos > -40: out[sym].append(Order(sym, state.order_depths[sym].best_bid(), -1))
        return out, 0, ""
