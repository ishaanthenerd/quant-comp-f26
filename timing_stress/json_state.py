"""Persists history through traderData (json.loads/dumps every tick) + numpy polyfit on the last 100 points."""
import json
import numpy as np
from datamodel import Order

class Trader:
    def run(self, state):
        st = json.loads(state.trader_data) if state.trader_data else {}
        out = {}
        for sym, od in state.order_depths.items():
            m = od.mid_price()
            if m is None: continue
            h = st.setdefault(sym, []); h.append(m); del h[:-500]
            if len(h) < 100: continue
            x = np.arange(100); y = np.array(h[-100:])
            slope, icpt = np.polyfit(x, y, 1)
            resid = y[-1] - (slope * 99 + icpt)
            pos = state.position.get(sym, 0)
            out[sym] = []
            if resid < -2 and pos < 40: out[sym].append(Order(sym, od.best_ask(), 1))
            if resid > 2 and pos > -40: out[sym].append(Order(sym, od.best_bid(), -1))
        return out, 0, json.dumps(st)
