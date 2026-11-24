"""
bots.py: bot interface + two REFERENCE bots used to test the engine.
The real bot roster (adversarial, insider, etc.) will be built on this same
interface once assets are designed.

A bot is stateful and lives for exactly one engine run (the engine builds
fresh bots each run, so runs are independent and deterministic).

Hooks, called by the engine each tick for each product:
  quotes(ctx)   -> (bids, asks): resting liquidity the participant can trade
                   against. Each is a list of (price, volume>0).
  on_fill(...)  -> called whenever the participant trades with this bot, so
                   the bot can REACT (skew quotes, widen spread, change mood).
  takes(ctx)    -> aggressive orders [(side, limit_price, qty)] that try to
                   trade against the participant's RESTING (unfilled) orders.
                   This is how passive participant quotes get filled.
All randomness must come from ctx.rng so runs stay reproducible.
"""
import math
import random
from dataclasses import dataclass
from typing import List, Optional, Tuple

Levels = List[Tuple[int, int]]


@dataclass
class Ctx:
    tick: int
    ts: int
    product: str
    fair: float                 # hidden fair value (bots may know it, participants don't)
    rng: random.Random
    best_bid: Optional[int] = None   # aggregated bot book, before participant trades
    best_ask: Optional[int] = None


class Bot:
    def quotes(self, ctx: Ctx) -> Tuple[Levels, Levels]:
        return [], []

    def on_fill(self, ctx: Ctx, bot_side: str, price: int, qty: int) -> None:
        """bot_side is 'buy' or 'sell': what the BOT did."""

    def takes(self, ctx: Ctx) -> List[Tuple[str, int, int]]:
        return []


class ReferenceMarketMaker(Bot):
    """Quotes around fair value. Reacts to the participant by:
       (1) skewing its quotes against its own inventory, and
       (2) widening its spread after being traded against (decays over time)."""

    def __init__(self, half_spread=2.0, size=10, skew=0.1, react=1.0, decay=0.9):
        self.half_spread, self.size, self.skew = half_spread, size, skew
        self.react, self.decay = react, decay
        self.inv = 0
        self.extra = 0.0

    def quotes(self, ctx):
        self.extra *= self.decay
        center = ctx.fair - self.skew * self.inv
        w = self.half_spread + self.extra
        bid, ask = math.floor(center - w), math.ceil(center + w)
        return ([(bid, self.size), (bid - 1, self.size)],
                [(ask, self.size), (ask + 1, self.size)])

    def on_fill(self, ctx, bot_side, price, qty):
        self.inv += qty if bot_side == "buy" else -qty
        self.extra += self.react * qty * 0.05


class ReferenceNoiseTaker(Bot):
    """Randomly lifts/hits the participant's resting orders if they are
    priced within `edge` of fair value."""

    def __init__(self, prob=0.3, max_size=3, edge=1.0):
        self.prob, self.max_size, self.edge = prob, max_size, edge

    def takes(self, ctx):
        if ctx.rng.random() > self.prob:
            return []
        qty = ctx.rng.randint(1, self.max_size)
        if ctx.rng.random() < 0.5:
            return [("buy", math.floor(ctx.fair + self.edge), qty)]
        return [("sell", math.ceil(ctx.fair - self.edge), qty)]
