"""
Practice market for local testing. This is NOT the real competition market:
the real products, price processes and bots are different (and secret), and the
real data uses different seeds. Use this to check that your code runs, respects
limits, and stays inside the time limit, not to tune parameters.
"""
import random

from bots import ReferenceMarketMaker, ReferenceNoiseTaker
from engine import Engine

PRODUCTS = ["ALPHA", "BETA", "GAMMA", "DELTA"]
LIMITS = {p: 50 for p in PRODUCTS}
N_SEGMENTS = 10
TICKS_PER_SEGMENT = 1000
PUBLIC_SEED = "practice"


def _fair_paths(seg):
    rng = random.Random(f"{PUBLIC_SEED}|fair|{seg}")
    out = {}
    for i, p in enumerate(PRODUCTS):
        x, path = 1000.0 + 100 * i, []
        for _ in range(TICKS_PER_SEGMENT):
            x += rng.gauss(0, 0.5)
            path.append(x)
        out[p] = path
    return out


def build(n_segments=N_SEGMENTS):
    """List of (segment_id, Engine). Each segment is an independent day; your Trader starts fresh in each."""
    factory = lambda: {p: [ReferenceMarketMaker(), ReferenceNoiseTaker()] for p in PRODUCTS}
    return [(s, Engine(PRODUCTS, LIMITS, _fair_paths(s), factory, seed=f"{PUBLIC_SEED}|bots|{s}"))
            for s in range(n_segments)]
