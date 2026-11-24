"""
scenario.py: the Halley's comet market (products in products.py, bots in bots.py).

Days (products.PUBLIC_DAYS / HIDDEN_DAYS):
  * Two PUBLIC days (CSV day -1 = May 15, day 0 = May 16). Their seed is fixed and their CSVs
    are handed out (starter_kit/data/). A "slice" job (every website submission) is graded
    on the public sample day 0, so teams can reproduce it locally.
  * Two HIDDEN days (CSV day 1, 2) seeded from COMP_SECRET_SEED, which only the trusted process
    has. A "full" job (the final ranking) runs both. They cannot be derived from the public data.
Every day starts with a fresh Trader, empty trader_data and zero positions.
"""
import os
import random

import products as P
from bots import Dumper, Filler, Insider, MarketMaker, Momentum, Prepper
from engine import Engine

PRODUCTS = P.PRODUCTS
LIMITS = dict(P.LIMITS)
PUBLIC_SEED = "qfc-halley-1910-public-v1"

# (lookback ticks, |net flow| threshold, probability, cooldown ticks) for the three townspeople
MOMENTUM = [(1, 14, 0.5, 6), (2, 20, 0.4, 10), (3, 28, 0.35, 15)]


def _secret():
    s = os.environ.get("COMP_SECRET_SEED")
    if s is None:
        return "dev-secret"          # local development only
    if not s:
        raise RuntimeError("COMP_SECRET_SEED is set but empty")
    return s


def bot_factory(seed):
    """Fresh bots for one run. Order matters: passive quoters first (market maker first of all),
    then takers in priority order (insider, prepper, filler, momentum)."""
    def rng(p, name):
        return random.Random(f"{seed}|{p}|{name}")
    out = {}
    for p in PRODUCTS:
        roster = [MarketMaker(p, rng(p, "mm"))]
        if p == P.TELESCOPE_GLASS:
            roster.append(Dumper(p, rng(p, "dumper")))
        if p == P.GAS_MASKS:
            roster.append(Insider(p, rng(p, "insider")))
        if p == P.TELESCOPE_GLASS:
            roster.append(Prepper(p, rng(p, "prepper")))
        roster.append(Filler(p, rng(p, "filler")))
        roster += [Momentum(p, rng(p, f"mom{k}"), *cfg) for k, cfg in enumerate(MOMENTUM)]
        out[p] = roster
    return out


def day_seed(spec):
    return PUBLIC_SEED if spec in P.PUBLIC_DAYS else _secret()


def build_day(spec, n_ticks=P.TICKS_PER_DAY):
    seed = day_seed(spec)
    paths = P.generate_day(spec, seed, n_ticks)
    return Engine(PRODUCTS, LIMITS, paths, bot_factory, seed=f"{seed}|bots|{spec.label}")


def days_for(kind):
    if kind == "slice":
        return [d for d in P.PUBLIC_DAYS if d.label == P.SAMPLE_DAY]
    if kind == "full":
        return list(P.HIDDEN_DAYS)
    if kind == "public":
        return list(P.PUBLIC_DAYS)
    raise ValueError(kind)


def build(kind):
    """Returns a list of (day_label, Engine)."""
    return [(d.label, build_day(d)) for d in days_for(kind)]
