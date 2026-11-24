"""
bots.py: HIDDEN bot roster for the Halley's comet scenario. Never ship to participants.

A bot lives for one engine run (one day). Each tick, for each product, the engine calls
  prepare(ctx)  -> the bot draws ALL its random numbers for the tick (a fixed count, whatever
                   happens), so a participant's actions can never shift a bot's random stream
  quotes(ctx)   -> (bids, asks): resting liquidity, lists of (price, volume > 0)
  takes(ctx)    -> aggressive orders [(side, limit_price, qty)], run after the participant
                   has traded; they hit the remaining bot quotes and the participant's resting orders
  on_fill(...)  -> the bot traded

Roster per product (see scenario.bot_factory):
  MarketMaker   all products. Quotes its fair-value guess +- a spread (2 levels). For the
                options its guess uses a MIS-SPECIFIED volatility (0.7x or 1.4x the true one).
                For SURVIVAL_KIT its guess is sum(components) + the merchant's error.
                At equal price its quotes fill before the participant's resting orders.
  Filler        all products. Trades badly with high variance around the TRUE value: sweeps
                through the book with a sloppy limit, and sometimes rests a quote on the wrong
                side of fair. Pennying the market maker / picking off its quotes earns money.
  Momentum x3   all products (the townspeople). Net aggressor volume over the last 1-3 ticks
                above a threshold -> "it's going up" -> buy (and vice versa).
  Insider       GAS_MASKS only. Knows the fair value ~40 ticks ahead and trades in its
                direction, ALWAYS with volume 6 or 7. No other bot ever uses 6 or 7.
  Dumper        TELESCOPE_GLASS only. A comet-doubter: during a dump it offers glass below 1000.
  Prepper       TELESCOPE_GLASS only. Buys up whatever the dumper leaves at the end of the tick.
"""
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import products as P

Levels = List[Tuple[int, int]]


@dataclass
class Ctx:
    tick: int
    ts: int
    product: str
    fair: float                    # TRUE fair value now
    day: "P.DayPaths"
    flow: List[int]                # signed aggressor volume per past tick (all trades, incl. the participant's)
    best_bid: Optional[int] = None  # bot book as shown to the participant (while quoting: best so far)
    best_ask: Optional[int] = None
    mm_bid: Optional[int] = None    # the market maker's best quotes this tick
    mm_ask: Optional[int] = None


class Bot:
    name = "bot"
    insider = False
    is_mm = False

    def __init__(self, product: str, rng):
        self.product, self.rng = product, rng
        self.vols = P.bot_volumes(product)

    def prepare(self, ctx: Ctx) -> None:
        pass

    def quotes(self, ctx: Ctx) -> Tuple[Levels, Levels]:
        return [], []

    def takes(self, ctx: Ctx) -> List[Tuple[str, int, int]]:
        return []

    def on_fill(self, ctx: Ctx, bot_side: str, price: int, qty: int) -> None:
        pass

    def _vol(self, u: float, lo: int = 0) -> int:
        vs = [v for v in self.vols if v >= lo] or self.vols
        return vs[min(int(u * len(vs)), len(vs) - 1)]


HALF_SPREAD = {P.TELESCOPE_GLASS: 3.0, P.COMET_PILLS: 2.0, P.GAS_MASKS: 4.0, P.SURVIVAL_KIT: 5.0,
               P.PILL_WARRANTS: 2.0, P.CALM_WEATHER_POLICIES: 15.0}


class MarketMaker(Bot):
    name = "market_maker"
    is_mm = True

    def __init__(self, product, rng, half_spread=None):
        super().__init__(product, rng)
        self.hs = HALF_SPREAD[product] if half_spread is None else half_spread
        self.inv = 0

    def prepare(self, ctx):
        self.r = (self.rng.gauss(0, 0.5), self.rng.random(), self.rng.random(),
                  self.rng.random(), self.rng.random())

    def guess(self, ctx):
        d, i = ctx.day, ctx.tick
        if self.product == P.SURVIVAL_KIT:
            return sum(d.fair[c][i] for c in P.BASKET) + d.kit_dev[i]
        if self.product == P.PILL_WARRANTS:
            return P.warrant_value(d.fair[P.COMET_PILLS][i], d.tte[i], d.mm_sigma())
        if self.product == P.CALM_WEATHER_POLICIES:
            return P.policy_value(d.fair[P.COMET_PILLS][i], d.tte[i], d.mm_sigma())
        return ctx.fair

    def quotes(self, ctx):
        g = self.guess(ctx) + self.r[0]
        bid, ask = math.floor(g - self.hs), math.ceil(g + self.hs)
        if self.product == P.TELESCOPE_GLASS and ctx.tick in ctx.day.dump_ticks:
            bid = min(bid, P.GLASS_FAIR - ctx.day.dump_ticks[ctx.tick] - 2)   # backs off during a dump
        bids = [(bid, self._vol(self.r[1])), (bid - 1, self._vol(self.r[2]))]
        asks = [(ask, self._vol(self.r[3])), (ask + 1, self._vol(self.r[4]))]
        return [b for b in bids if b[0] >= 1], asks

    def on_fill(self, ctx, bot_side, price, qty):
        self.inv += qty if bot_side == "buy" else -qty


class Filler(Bot):
    name = "filler"

    def __init__(self, product, rng, take_prob=0.08, rest_prob=0.04):
        super().__init__(product, rng)
        self.hs = HALF_SPREAD[product]
        self.take_prob, self.rest_prob = take_prob, rest_prob

    def prepare(self, ctx):
        r = self.rng
        self.r = (r.random(), r.random(), abs(r.gauss(0, 3 * self.hs)), r.random(),
                  r.random(), r.random(), r.random(), r.random())

    def quotes(self, ctx):
        _, _, _, _, act, side, off, vu = self.r
        if act >= self.rest_prob:
            return [], []
        off = 0.5 + off * max(self.hs - 1.0, 0.5)
        # ctx.best_bid / best_ask here are the best quotes posted so far this tick (never cross them)
        if side < 0.5:                       # bid ABOVE fair, inside the market maker's spread
            px = round(ctx.fair + off)
            if ctx.best_ask is not None:
                px = min(px, ctx.best_ask - 1)
            if px < 1 or (ctx.mm_bid is not None and px <= ctx.mm_bid):
                return [], []
            return [(px, self._vol(vu))], []
        px = round(ctx.fair - off)           # ask BELOW fair, inside the market maker's spread
        if ctx.best_bid is not None:
            px = max(px, ctx.best_bid + 1)
        if ctx.mm_ask is not None and px >= ctx.mm_ask:
            return [], []
        return [], [(px, self._vol(vu))]

    def takes(self, ctx):
        act, side, off, vu = self.r[:4]
        if act >= self.take_prob:
            return []
        slop = self.hs + off
        if side < 0.5:
            return [("buy", math.floor(ctx.fair + slop), self._vol(vu))]
        return [("sell", max(1, math.ceil(ctx.fair - slop)), self._vol(vu))]


class Momentum(Bot):
    name = "momentum"

    def __init__(self, product, rng, lookback, threshold, prob, cooldown):
        super().__init__(product, rng)
        self.lookback, self.threshold, self.prob, self.cooldown = lookback, threshold, prob, cooldown
        self.wait = 0

    def prepare(self, ctx):
        self.r = (self.rng.random(), self.rng.random())

    def takes(self, ctx):
        if self.wait > 0:
            self.wait -= 1
            return []
        net = sum(ctx.flow[-self.lookback:])
        if abs(net) < self.threshold or self.r[0] >= self.prob:
            return []
        self.wait = self.cooldown
        q = self._vol(self.r[1])
        if net > 0 and ctx.best_ask is not None:
            return [("buy", ctx.best_ask + 1, q)]
        if net < 0 and ctx.best_bid is not None:
            return [("sell", ctx.best_bid - 1, q)]
        return []


class Insider(Bot):
    """Knows GAS_MASKS' fair value `horizon` ticks ahead. Always volume 6 or 7."""
    name = "insider"
    insider = True

    def __init__(self, product, rng, horizon=40, rel_threshold=0.03, prob=0.15, cooldown=15):
        super().__init__(product, rng)
        self.horizon, self.rel_threshold, self.prob, self.cooldown = horizon, rel_threshold, prob, cooldown
        self.wait = 0

    def prepare(self, ctx):
        self.r = (self.rng.random(), self.rng.random())

    def takes(self, ctx):
        if self.wait > 0:
            self.wait -= 1
            return []
        if self.r[0] >= self.prob:
            return []
        path = ctx.day.fair[self.product]
        move = path[min(ctx.tick + self.horizon, len(path) - 1)] - ctx.fair
        if abs(move) < self.rel_threshold * ctx.fair:
            return []
        self.wait = self.cooldown
        q = P.INSIDER_VOLUMES[0] if self.r[1] < 0.5 else P.INSIDER_VOLUMES[1]
        if move > 0 and ctx.best_ask is not None:
            return [("buy", ctx.best_ask + 2, q)]
        if move < 0 and ctx.best_bid is not None:
            return [("sell", ctx.best_bid - 2, q)]
        return []


class Dumper(Bot):
    name = "dumper"

    def prepare(self, ctx):
        self.r = (self.rng.random(),)

    def quotes(self, ctx):
        depth = ctx.day.dump_ticks.get(ctx.tick)
        if depth is None:
            return [], []
        return [], [(P.GLASS_FAIR - depth, self._vol(self.r[0], lo=12))]


class Prepper(Bot):
    name = "prepper"

    def takes(self, ctx):
        if ctx.tick in ctx.day.dump_ticks:
            return [("buy", P.GLASS_FAIR - 1, max(self.vols))]
        return []
