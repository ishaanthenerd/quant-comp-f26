"""Engine speed + sanity on the real scenario (in-process, no sandbox):  python3 bench.py"""
import math
import time

import scenario
from datamodel import Order


class DoNothing:
    def run(self, s): return {}, 0, ""


class NaiveTaker:
    """Crosses the spread constantly."""
    def run(self, s):
        out = {}
        for sym, od in s.order_depths.items():
            if od.best_ask() is None or od.best_bid() is None:
                continue
            if s.position.get(sym, 0) < 5:
                out[sym] = [Order(sym, od.best_ask(), 1)]
            else:
                out[sym] = [Order(sym, od.best_bid(), -1)]
        return out, 0, ""


class PassiveQuoter:
    """Posts one tick inside the spread; only profits if bots fill resting orders."""
    def run(self, s):
        out = {}
        for sym, od in s.order_depths.items():
            b, a = od.best_bid(), od.best_ask()
            if b is None or a is None or a - b < 3:
                continue
            pos, lim = s.position.get(sym, 0), s.position_limits[sym]
            out[sym] = []
            if pos < lim - 2: out[sym].append(Order(sym, b + 1, 2))
            if pos > -lim + 2: out[sym].append(Order(sym, a - 1, -2))
        return out, 0, ""


class Overreach:
    def run(self, s): return {x: [Order(x, 10**6, 999)] for x in s.order_depths}, 0, ""


class Crasher:
    def run(self, s): raise RuntimeError("boom")


if __name__ == "__main__":
    for kind in ("slice", "full"):
        for label, eng in scenario.build(kind):
            for T in (DoNothing, NaiveTaker, PassiveQuoter, Overreach, Crasher):
                t = time.perf_counter()
                r = eng.run(T())
                print(f"{kind:5s} day {label:2d} {T.__name__:13s} pnl={r.final_pnl:10.1f} viol={r.limit_violations:5d} "
                      f"err={r.errors:5d} mdd={r.max_drawdown:8.1f} {time.perf_counter() - t:.2f}s")
    eng = scenario.build("slice")[0][1]
    a, b = eng.run(PassiveQuoter()).final_pnl, eng.run(PassiveQuoter()).final_pnl
    print("deterministic:", a == b)
