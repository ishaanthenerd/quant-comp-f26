"""Correctness + speed checks for engine.py with reactive reference bots."""
import math, random, time
from datamodel import Order
from engine import Engine
from bots import ReferenceMarketMaker, ReferenceNoiseTaker

PRODUCTS = ["ALPHA", "BETA", "GAMMA", "DELTA"]
LIMITS = {p: 50 for p in PRODUCTS}

def fair_paths(n, seed=0):
    rng = random.Random(seed)
    out = {}
    for i, p in enumerate(PRODUCTS):
        x, path = 1000.0 + 100 * i, []
        for _ in range(n):
            x += rng.gauss(0, 0.5)
            path.append(x)
        out[p] = path
    return out

def factory(react=1.0):
    return lambda: {p: [ReferenceMarketMaker(react=react), ReferenceNoiseTaker()] for p in PRODUCTS}

class DoNothing:
    def run(self, s): return {}, 0, ""

class NaiveTaker:
    """Crosses the spread constantly."""
    def run(self, s):
        out = {}
        for sym, od in s.order_depths.items():
            if s.position.get(sym, 0) < 10:
                out[sym] = [Order(sym, od.best_ask(), 1)]
            else:
                out[sym] = [Order(sym, od.best_bid(), -1)]
        return out, 0, ""

class PassiveQuoter:
    """Posts inside the spread; only profits if bots fill resting orders."""
    def run(self, s):
        out = {}
        for sym, od in s.order_depths.items():
            m = od.mid_price()
            if m is None: continue
            pos = s.position.get(sym, 0)
            out[sym] = []
            if pos < 40: out[sym].append(Order(sym, math.floor(m), 2))
            if pos > -40: out[sym].append(Order(sym, math.ceil(m), -2))
        return out, 0, ""

class Overreach:
    def run(self, s): return {x: [Order(x, 10**6, 999)] for x in s.order_depths}, 0, ""

class Crasher:
    def run(self, s): raise RuntimeError("boom")

if __name__ == "__main__":
    N = 10_000
    fp = fair_paths(N)
    eng = Engine(PRODUCTS, LIMITS, fp, factory(1.0), seed=1)
    for T in (DoNothing, NaiveTaker, PassiveQuoter, Overreach, Crasher):
        t = time.perf_counter()
        r = eng.run(T())
        print(f"{T.__name__:13s} pnl={r.final_pnl:10.1f} pos={list(r.final_positions.values())} "
              f"viol={r.limit_violations} err={r.errors} mdd={r.max_drawdown:8.1f} {time.perf_counter()-t:.2f}s")

    # Determinism: identical results on re-run
    a = eng.run(NaiveTaker()).final_pnl; b = eng.run(NaiveTaker()).final_pnl
    print("deterministic:", a == b)

    # Reaction: spread-crossing should cost MORE when the market maker widens after being hit
    for react in (0.0, 1.0, 3.0):
        e = Engine(PRODUCTS, LIMITS, fp, factory(react), seed=1)
        print(f"react={react}: NaiveTaker pnl = {e.run(NaiveTaker()).final_pnl:.1f}")
