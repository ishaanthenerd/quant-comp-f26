"""
engine.py: matching engine / backtester with REACTIVE bots.

Per tick, per product:
  1. Bots post quotes (bots.quotes) around the hidden fair value.
  2. The participant sees the aggregated book and returns orders.
  3. Orders are validated; if a product's orders would breach the position
     limit, ALL of that product's orders are rejected (IMC-style).
  4. Marketable orders trade against bot quotes at the bots' prices
     (price priority, then bot order). Each bot is notified via on_fill so it
     can react on later ticks.
  5. Unfilled remainder of participant orders rests for the rest of this tick
     and can be hit by bot takers (bots.takes). Then it is cancelled.
  6. PnL is marked to the HIDDEN FAIR VALUE (not the book mid), so a
     participant can't manipulate the mark by pushing reactive bots around.

Determinism: all randomness comes from per-product RNGs seeded from `seed`;
bots are rebuilt by `bot_factory` at the start of every run.
"""
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List

from bots import Bot, Ctx
from datamodel import Listing, Observation, Order, OrderDepth, Trade, TradingState

MAX_TRADER_DATA = 50_000
TICK_TIME_LIMIT = 0.100  # seconds; hard enforcement happens in the sandbox


@dataclass
class Result:
    final_pnl: float
    pnl_by_product: Dict[str, float]
    final_positions: Dict[str, int]
    pnl_series: List[float]
    max_drawdown: float
    limit_violations: int
    slow_ticks: int
    errors: int
    logs: List[str] = field(default_factory=list)


class Engine:
    def __init__(self, products: List[str], limits: Dict[str, int],
                 fair_paths: Dict[str, List[float]],
                 bot_factory: Callable[[], Dict[str, List[Bot]]], seed: int = 0):
        self.products, self.limits = products, limits
        self.fair, self.bot_factory, self.seed = fair_paths, bot_factory, seed
        self.listings = {p: Listing(p, p) for p in products}
        self.n_ticks = len(next(iter(fair_paths.values())))

    def run(self, trader, tick_size: int = 100) -> Result:
        bots = self.bot_factory()
        rngs = {p: random.Random(f"{self.seed}-{p}") for p in self.products}
        pos = {p: 0 for p in self.products}
        cash = {p: 0.0 for p in self.products}
        trader_data, own_prev = "", {}
        series: List[float] = []
        violations = slow = errors = 0

        for i in range(self.n_ticks):
            ts = i * tick_size
            ctxs, lv_b, lv_a, depths = {}, {}, {}, {}

            # 1. bots quote
            for p in self.products:
                ctx = Ctx(i, ts, p, self.fair[p][i], rngs[p])
                bl, al = [], []
                for bi, bot in enumerate(bots.get(p, [])):
                    bids, asks = bot.quotes(ctx)
                    bl += [[int(pr), int(v), bi] for pr, v in bids if v > 0]
                    al += [[int(pr), int(v), bi] for pr, v in asks if v > 0]
                bl.sort(key=lambda l: (-l[0], l[2]))
                al.sort(key=lambda l: (l[0], l[2]))
                ctx.best_bid = bl[0][0] if bl else None
                ctx.best_ask = al[0][0] if al else None
                ctxs[p], lv_b[p], lv_a[p] = ctx, bl, al
                od = OrderDepth()
                for pr, v, _ in bl:
                    od.buy_orders[pr] = od.buy_orders.get(pr, 0) + v
                for pr, v, _ in al:
                    od.sell_orders[pr] = od.sell_orders.get(pr, 0) - v
                depths[p] = od

            # 2. participant acts
            state = TradingState(trader_data, ts, self.listings, depths, own_prev, {},
                                 dict(pos), dict(self.limits), Observation())
            t0 = time.perf_counter()
            try:
                orders_by_sym, _conv, new_td = trader.run(state)
            except Exception:
                errors += 1
                orders_by_sym, new_td = {}, trader_data
            if time.perf_counter() - t0 > TICK_TIME_LIMIT:
                slow += 1
                orders_by_sym = {}
            if isinstance(new_td, str) and len(new_td) <= MAX_TRADER_DATA:
                trader_data = new_td

            # 3-5. validate, match, rest, bot takers
            own_now: Dict[str, List[Trade]] = {}
            for sym, orders in (orders_by_sym or {}).items():
                if sym not in pos:
                    continue
                valid = [o for o in orders if isinstance(o, Order) and o.symbol == sym
                         and type(o.price) is int and type(o.quantity) is int
                         and o.quantity != 0]
                buys = sum(o.quantity for o in valid if o.quantity > 0)
                sells = sum(-o.quantity for o in valid if o.quantity < 0)
                if pos[sym] + buys > self.limits[sym] or pos[sym] - sells < -self.limits[sym]:
                    violations += 1
                    continue
                ctx, sb = ctxs[sym], bots.get(sym, [])
                rest_buys, rest_sells = [], []
                for o in valid:
                    qty = abs(o.quantity)
                    if o.quantity > 0:
                        for lvl in lv_a[sym]:
                            if qty == 0 or lvl[0] > o.price:
                                break
                            f = min(qty, lvl[1])
                            if f <= 0:
                                continue
                            lvl[1] -= f; qty -= f
                            pos[sym] += f; cash[sym] -= f * lvl[0]
                            sb[lvl[2]].on_fill(ctx, "sell", lvl[0], f)
                            own_now.setdefault(sym, []).append(Trade(sym, lvl[0], f, "SUBMISSION", None, ts))
                        if qty:
                            rest_buys.append([o.price, qty])
                    else:
                        for lvl in lv_b[sym]:
                            if qty == 0 or lvl[0] < o.price:
                                break
                            f = min(qty, lvl[1])
                            if f <= 0:
                                continue
                            lvl[1] -= f; qty -= f
                            pos[sym] -= f; cash[sym] += f * lvl[0]
                            sb[lvl[2]].on_fill(ctx, "buy", lvl[0], f)
                            own_now.setdefault(sym, []).append(Trade(sym, lvl[0], f, None, "SUBMISSION", ts))
                        if qty:
                            rest_sells.append([o.price, qty])

                # bot takers hit what is still resting
                rest_buys.sort(key=lambda r: -r[0])
                rest_sells.sort(key=lambda r: r[0])
                for bot in sb:
                    for side, limit, q in bot.takes(ctx):
                        if side == "buy":      # bot lifts participant asks
                            for r in rest_sells:
                                if q == 0 or r[0] > limit:
                                    break
                                f = min(q, r[1])
                                if f <= 0:
                                    continue
                                r[1] -= f; q -= f
                                pos[sym] -= f; cash[sym] += f * r[0]
                                bot.on_fill(ctx, "buy", r[0], f)
                                own_now.setdefault(sym, []).append(Trade(sym, r[0], f, None, "SUBMISSION", ts))
                        else:                  # bot hits participant bids
                            for r in rest_buys:
                                if q == 0 or r[0] < limit:
                                    break
                                f = min(q, r[1])
                                if f <= 0:
                                    continue
                                r[1] -= f; q -= f
                                pos[sym] += f; cash[sym] -= f * r[0]
                                bot.on_fill(ctx, "sell", r[0], f)
                                own_now.setdefault(sym, []).append(Trade(sym, r[0], f, "SUBMISSION", None, ts))
            own_prev = own_now

            series.append(sum(cash[p] + pos[p] * self.fair[p][i] for p in self.products))

        last = self.n_ticks - 1
        by_prod = {p: cash[p] + pos[p] * self.fair[p][last] for p in self.products}
        peak, mdd = float("-inf"), 0.0
        for v in series:
            peak = max(peak, v)
            mdd = max(mdd, peak - v)
        return Result(sum(by_prod.values()), by_prod, dict(pos), series, mdd,
                      violations, slow, errors)
