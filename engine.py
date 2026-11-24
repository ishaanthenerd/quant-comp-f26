"""
engine.py: matching engine. Runs one day of the market with a participant in it.

Per tick, per product:
  1. Every bot draws its random numbers for the tick (fixed count; see bots.py).
  2. Passive bots quote (market maker first). The book shown to the participant is at most
     MAX_LEVELS (3) price levels per side, and at most MAX_BOTS_PER_SIDE (3) bots act on a side.
  3. The participant sees the book and the previous tick's own/market trades and returns orders.
     If a product's orders could breach the position limit, ALL of that product's orders are
     rejected (IMC-style). Marketable orders trade against bot quotes at the bots' prices.
  4. The participant's unfilled remainder rests. Bot takers (insider, prepper, filler, momentum)
     then trade against what is resting: bot quotes AND the participant's orders, best price first;
     AT EQUAL PRICE, BOT QUOTES (the market maker) FILL BEFORE THE PARTICIPANT.
     Bot-vs-bot fills only use the volumes bots are allowed (products.volume_allowed), so the
     market-trade tape never shows a 6 or 7 except from the insider.
  5. Bot-vs-bot trades go on the tape (state.market_trades next tick; the trades CSV). Everything
     left is cancelled.
  6. PnL is marked to the TRUE fair value (options: true-volatility model value; kit: sum of its
     contents), so nobody can move the mark by pushing bots around.

Determinism: all randomness comes from per-(product, bot) RNGs seeded from `seed`, drawn a fixed
number of times per tick; bots are rebuilt by `bot_factory` at the start of every run.
"""
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import products as P
from bots import Bot, Ctx
from datamodel import Listing, Observation, Order, OrderDepth, Trade, TradingState

MAX_TRADER_DATA = 50_000
TICK_TIME_LIMIT = 0.100  # seconds; hard enforcement happens in the sandbox
MAX_LEVELS = 3
MAX_BOTS_PER_SIDE = 3
SUBMISSION = "SUBMISSION"
PLAYER = -1               # owner id for the participant's resting orders


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


class Recorder:
    """Collects what a CSV export needs: the book each tick and the bot-vs-bot tape."""
    def __init__(self):
        self.books = []    # (ts, product, [(px, vol)] bids best-first, [(px, vol)] asks best-first)
        self.trades = []   # (ts, product, price, qty)
        self.trade_bots = []   # (taker bot name, maker bot name, taker side), parallel to trades; never exported


class Engine:
    def __init__(self, products: List[str], limits: Dict[str, int], day: "P.DayPaths",
                 bot_factory: Callable[[str], Dict[str, List[Bot]]], seed: str = "0"):
        self.products, self.limits, self.day = products, limits, day
        self.fair = day.fair
        self.bot_factory, self.seed = bot_factory, seed
        self.listings = {p: Listing(p, p, P.CURRENCY) for p in products}
        self.n_ticks = day.n

    def run(self, trader, tick_size: int = P.TIMESTAMP_STEP, recorder: Optional[Recorder] = None) -> Result:
        bots = self.bot_factory(self.seed)
        pos = {p: 0 for p in self.products}
        cash = {p: 0.0 for p in self.products}
        flow = {p: [] for p in self.products}
        trader_data, own_prev, mkt_prev = "", {}, {}
        series: List[float] = []
        violations = slow = errors = 0
        day = self.day

        for i in range(self.n_ticks):
            ts = i * tick_size
            ctxs, books, depths, passive_sides = {}, {}, {}, {}

            # 1-2. bots prepare and quote
            for p in self.products:
                ctx = Ctx(i, ts, p, self.fair[p][i], day, flow[p])
                pb = bots.get(p, [])
                for b in pb:
                    b.prepare(ctx)
                bl, al, nb, na = [], [], 0, 0
                for bi, b in enumerate(pb):
                    bids, asks = b.quotes(ctx)
                    bids = [(int(px), int(v)) for px, v in bids if v > 0]
                    asks = [(int(px), int(v)) for px, v in asks if v > 0]
                    if bids and nb < MAX_BOTS_PER_SIDE:
                        nb += 1
                        bl += [[px, v, bi] for px, v in bids]
                    if asks and na < MAX_BOTS_PER_SIDE:
                        na += 1
                        al += [[px, v, bi] for px, v in asks]
                    if b.is_mm:
                        ctx.mm_bid = max((x[0] for x in bids), default=None)
                        ctx.mm_ask = min((x[0] for x in asks), default=None)
                    if bl:
                        ctx.best_bid = max(x[0] for x in bl)
                    if al:
                        ctx.best_ask = min(x[0] for x in al)
                bl = _keep_levels(bl, buy=True)
                al = _keep_levels(al, buy=False)
                ctx.best_bid = bl[0][0] if bl else None
                ctx.best_ask = al[0][0] if al else None
                od = OrderDepth()
                for px, v, _ in bl:
                    od.buy_orders[px] = od.buy_orders.get(px, 0) + v
                for px, v, _ in al:
                    od.sell_orders[px] = od.sell_orders.get(px, 0) - v
                ctxs[p], books[p], depths[p], passive_sides[p] = ctx, (bl, al), od, (nb, na)
                if recorder is not None:
                    recorder.books.append((ts, p, sorted(od.buy_orders.items(), reverse=True),
                                           [(px, -v) for px, v in sorted(od.sell_orders.items())]))

            # 3. participant acts
            state = TradingState(trader_data, ts, self.listings, depths, own_prev, mkt_prev,
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
            if not isinstance(orders_by_sym, dict):
                orders_by_sym = {}

            own_now: Dict[str, List[Trade]] = {}
            mkt_now: Dict[str, List[Trade]] = {}
            for p in self.products:
                ctx, pb = ctxs[p], bots.get(p, [])
                bl, al = books[p]
                tick_flow = 0
                rest_buys, rest_sells = [], []     # [price, qty, PLAYER]

                orders = orders_by_sym.get(p) or []
                valid = [o for o in orders if isinstance(o, Order) and o.symbol == p
                         and type(o.price) is int and type(o.quantity) is int and o.quantity != 0]
                buys = sum(o.quantity for o in valid if o.quantity > 0)
                sells = sum(-o.quantity for o in valid if o.quantity < 0)
                if valid and (pos[p] + buys > self.limits[p] or pos[p] - sells < -self.limits[p]):
                    violations += 1
                    valid = []
                for o in valid:
                    qty = abs(o.quantity)
                    buy = o.quantity > 0
                    for lvl in (al if buy else bl):
                        if qty == 0 or (lvl[0] > o.price if buy else lvl[0] < o.price):
                            break
                        f = min(qty, lvl[1])
                        if f <= 0:
                            continue
                        lvl[1] -= f
                        qty -= f
                        if buy:
                            pos[p] += f; cash[p] -= f * lvl[0]; tick_flow += f
                            own_now.setdefault(p, []).append(Trade(p, lvl[0], f, SUBMISSION, None, ts))
                        else:
                            pos[p] -= f; cash[p] += f * lvl[0]; tick_flow -= f
                            own_now.setdefault(p, []).append(Trade(p, lvl[0], f, None, SUBMISSION, ts))
                        pb[lvl[2]].on_fill(ctx, "sell" if buy else "buy", lvl[0], f)
                    if qty:
                        (rest_buys if buy else rest_sells).append([o.price, qty, PLAYER])

                # 4. bot takers, capped so at most MAX_BOTS_PER_SIDE bots act on each side
                nb, na = passive_sides[p]
                for bi, b in enumerate(pb):
                    for side, limit, q in b.takes(ctx):
                        if side == "buy":
                            if nb >= MAX_BOTS_PER_SIDE:
                                continue
                            nb += 1
                            # asks: best price first; at equal price bots (priority 0) before the player (1)
                            book = sorted(al + rest_sells, key=lambda l: (l[0], l[2] == PLAYER))
                            ok = lambda px: px <= limit
                        else:
                            if na >= MAX_BOTS_PER_SIDE:
                                continue
                            na += 1
                            book = sorted(bl + rest_buys, key=lambda l: (-l[0], l[2] == PLAYER))
                            ok = lambda px: px >= limit
                        for lvl in book:
                            if q <= 0 or not ok(lvl[0]):
                                break
                            if lvl[1] <= 0 or lvl[2] == bi:
                                continue
                            if lvl[2] == PLAYER:
                                f = min(q, lvl[1])
                            else:
                                f = _allowed_fill(p, min(q, lvl[1]), b.insider)
                                if f == 0:
                                    continue
                            lvl[1] -= f
                            q -= f
                            tick_flow += f if side == "buy" else -f
                            b.on_fill(ctx, side, lvl[0], f)
                            if lvl[2] == PLAYER:
                                if side == "buy":     # bot lifted the participant's ask
                                    pos[p] -= f; cash[p] += f * lvl[0]
                                    own_now.setdefault(p, []).append(Trade(p, lvl[0], f, None, SUBMISSION, ts))
                                else:
                                    pos[p] += f; cash[p] -= f * lvl[0]
                                    own_now.setdefault(p, []).append(Trade(p, lvl[0], f, SUBMISSION, None, ts))
                            else:
                                pb[lvl[2]].on_fill(ctx, "sell" if side == "buy" else "buy", lvl[0], f)
                                mkt_now.setdefault(p, []).append(Trade(p, lvl[0], f, None, None, ts))
                                if recorder is not None:
                                    recorder.trades.append((ts, p, lvl[0], f))
                                    recorder.trade_bots.append((b.name, pb[lvl[2]].name, side))
                flow[p].append(tick_flow)
                if len(flow[p]) > 16:
                    del flow[p][:-8]
            own_prev, mkt_prev = own_now, mkt_now

            series.append(sum(cash[p] + pos[p] * self.fair[p][i] for p in self.products))

        last = self.n_ticks - 1
        by_prod = {p: cash[p] + pos[p] * self.fair[p][last] for p in self.products}
        peak, mdd = float("-inf"), 0.0
        for v in series:
            peak = max(peak, v)
            mdd = max(mdd, peak - v)
        return Result(sum(by_prod.values()), by_prod, dict(pos), series, mdd,
                      violations, slow, errors)


def _keep_levels(levels, buy):
    """Sort best-first (bot order breaks ties) and keep the best MAX_LEVELS distinct prices."""
    levels.sort(key=(lambda l: (-l[0], l[2])) if buy else (lambda l: (l[0], l[2])))
    prices = []
    for l in levels:
        if l[0] not in prices:
            prices.append(l[0])
    keep = set(prices[:MAX_LEVELS])
    return [l for l in levels if l[0] in keep]


def _allowed_fill(product, f, insider):
    """Largest volume <= f that this bot is allowed to trade with another bot (0 if none)."""
    while f > 0 and not P.volume_allowed(product, f, insider):
        f -= 1
    return f
