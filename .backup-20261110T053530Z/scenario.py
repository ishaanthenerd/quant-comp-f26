"""
scenario.py: PLACEHOLDER scenario (random-walk fair values + the reference bots).
Replace build() with the real asset/bot/data generator once those exist.

The hidden seed comes from the COMP_SECRET_SEED environment variable of the
TRUSTED process only. The full set is N_SEGMENTS independent segments; the
slice is ONE of them, chosen by the secret, so the slice is a true sample of
the hidden set rather than a hand-picked window.
"""
import hashlib
import os
import random

from bots import ReferenceMarketMaker, ReferenceNoiseTaker
from engine import Engine

PRODUCTS = ["ALPHA", "BETA", "GAMMA", "DELTA"]
LIMITS = {p: 50 for p in PRODUCTS}
N_SEGMENTS = 10
TICKS_PER_SEGMENT = 1000


def _secret():
    s = os.environ.get("COMP_SECRET_SEED")
    if s is None:
        return "dev-secret"          # local development only
    if not s:
        raise RuntimeError("COMP_SECRET_SEED is set but empty")
    return s


def slice_index():
    return int(hashlib.sha256((_secret() + "|slice").encode()).hexdigest(), 16) % N_SEGMENTS


def _fair_paths(seg):
    rng = random.Random(f"{_secret()}|fair|{seg}")
    out = {}
    for i, p in enumerate(PRODUCTS):
        x, path = 1000.0 + 100 * i, []
        for _ in range(TICKS_PER_SEGMENT):
            x += rng.gauss(0, 0.5)
            path.append(x)
        out[p] = path
    return out


def build(kind):
    """Returns a list of (segment_id, Engine)."""
    if kind not in ("slice", "full"):
        raise ValueError(kind)
    segs = [slice_index()] if kind == "slice" else list(range(N_SEGMENTS))
    factory = lambda: {p: [ReferenceMarketMaker(), ReferenceNoiseTaker()] for p in PRODUCTS}
    return [(s, Engine(PRODUCTS, LIMITS, _fair_paths(s), factory, seed=f"{_secret()}|bots|{s}"))
            for s in segs]
