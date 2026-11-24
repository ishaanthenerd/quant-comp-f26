"""
products.py: HIDDEN product definitions, price processes and option maths for the
"Halley's comet, Texas, May 1910" scenario. Never ship this file to participants.

Units
-----
All prices are integer ticks of ONE CENT. Lore dollar amounts are multiplied by 100:
    COMET_PILLS ~ $11       -> ~1100
    PILL_WARRANTS strike $12 -> 1200
    CALM_WEATHER_POLICIES pays $100 if pills < $8 -> pays 10000 if pills < 800
TELESCOPE_GLASS's fixed fair value of 1000 is used as-is (1000 cents = $10).

Calendar
--------
Every simulated day is one session, 9 am to 4 pm, with TICKS_PER_DAY ticks
(timestamps 0, 100, ..., 999900, as in IMC Prosperity). Both options expire at
9 am on May 18, 1910. Time is measured in calendar days, so a session that opens
on May 16 starts 2.0 days before expiry and ends 1 + 17/24 days before it.

Option settlement / marking (the rule the engine uses)
-------------------------------------------------------
No session reaches expiry, so end-of-run option positions are marked at the
TRUE-volatility model value with the time to expiry remaining at the close:
    PILL_WARRANTS          Black-Scholes call, r = 0
    CALM_WEATHER_POLICIES  cash-or-nothing put = PAYOUT * N(-d2)
The pills process is a zero-drift (martingale) GBM with the same constant sigma the
model uses, so this mark is exactly the expected payoff at expiry: it is the
fair, low-variance version of "simulate to expiry and pay intrinsic".
At T = 0 the same functions return the intrinsic payoff max(S-K, 0) and
PAYOUT * 1{S < 800} (strictly below: exactly 800 pays nothing).
"""
import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Set

TELESCOPE_GLASS = "TELESCOPE_GLASS"
COMET_PILLS = "COMET_PILLS"
GAS_MASKS = "GAS_MASKS"
SURVIVAL_KIT = "SURVIVAL_KIT"
PILL_WARRANTS = "PILL_WARRANTS"
CALM_WEATHER_POLICIES = "CALM_WEATHER_POLICIES"

PRODUCTS = [TELESCOPE_GLASS, COMET_PILLS, GAS_MASKS, SURVIVAL_KIT, PILL_WARRANTS, CALM_WEATHER_POLICIES]
BASKET = {TELESCOPE_GLASS: 1, COMET_PILLS: 1, GAS_MASKS: 1}

# Position limits (see README "Products"): sized so a full kit position can be hedged
# with components, and so the binary (payout 10000) is not a lottery ticket.
LIMITS = {TELESCOPE_GLASS: 50, COMET_PILLS: 50, GAS_MASKS: 50, SURVIVAL_KIT: 25,
          PILL_WARRANTS: 100, CALM_WEATHER_POLICIES: 10}

CURRENCY = "CENTS"
TICKS_PER_DAY = 10_000
TIMESTAMP_STEP = 100
SESSION_DAYS = 7 / 24                     # 9 am to 4 pm, in calendar days
GLASS_FAIR = 1000
WARRANT_STRIKE = 1200
POLICY_THRESHOLD = 800
POLICY_PAYOUT = 10_000

# ---- volumes: the insider is the only bot that ever uses 6 or 7 ------------------
INSIDER_VOLUMES = (6, 7)


def bot_volumes(product: str) -> List[int]:
    """Volumes a NON-insider bot may quote or trade. 8..25 everywhere (6..25 minus the insider's 6 and 7);
    GAS_MASKS uses its product rule 5..20, again minus 6 and 7."""
    if product == GAS_MASKS:
        return [5] + list(range(8, 21))
    return list(range(8, 26))


_ALLOWED = {p: frozenset(bot_volumes(p)) for p in PRODUCTS}


def volume_allowed(product: str, v: int, insider: bool = False) -> bool:
    return v in INSIDER_VOLUMES if insider else v in _ALLOWED[product]


# ---- option maths ------------------------------------------------------------------
def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def bs_call(S: float, K: float, T: float, sigma: float) -> float:
    """Black-Scholes call with r = 0; T and sigma in calendar days. T <= 0 gives intrinsic."""
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * v * v) / v
    return S * norm_cdf(d1) - K * norm_cdf(d1 - v)


def binary_put(S: float, K: float, T: float, sigma: float, payout: float = POLICY_PAYOUT) -> float:
    """Cash-or-nothing put: pays `payout` if S_T < K. Value payout * N(-d2), r = 0. T <= 0 gives intrinsic."""
    if T <= 0 or sigma <= 0:
        return payout if S < K else 0.0
    v = sigma * math.sqrt(T)
    d2 = (math.log(S / K) - 0.5 * v * v) / v
    return payout * norm_cdf(-d2)


def warrant_value(S, T, sigma):
    return bs_call(S, WARRANT_STRIKE, T, sigma)


def policy_value(S, T, sigma):
    return binary_put(S, POLICY_THRESHOLD, T, sigma)


def settle_at_expiry(pills_price: float) -> Dict[str, float]:
    """Intrinsic payoffs at expiry (used by tests; sessions never reach expiry)."""
    return {PILL_WARRANTS: max(pills_price - WARRANT_STRIKE, 0.0),
            CALM_WEATHER_POLICIES: POLICY_PAYOUT if pills_price < POLICY_THRESHOLD else 0.0}


# ---- days ----------------------------------------------------------------------------
@dataclass(frozen=True)
class DaySpec:
    label: int            # the CSV `day` column
    date: str
    tte_open: float       # calendar days from the 9 am open to expiry (9 am May 18)


PUBLIC_DAYS = [DaySpec(-1, "1910-05-15", 3.0), DaySpec(0, "1910-05-16", 2.0)]
# Hidden days mirror the public calendar (independent histories of the same two sessions),
# so the final run is drawn from the same distribution participants backtested on.
HIDDEN_DAYS = [DaySpec(1, "1910-05-15", 3.0), DaySpec(2, "1910-05-16", 2.0)]
SAMPLE_DAY = 0            # the public day graded by "slice" submissions (May 16)


@dataclass
class DayPaths:
    """Exogenous data for one day: true fair values plus the hidden event schedule."""
    spec: DaySpec
    fair: Dict[str, List[float]]
    tte: List[float]
    pills_sigma: float            # TRUE sigma (per sqrt calendar day), constant all day
    mm_vol_mult: float            # the options market maker uses pills_sigma * this
    kit_dev: List[float]          # merchant's error: kit quotes centre on sum(components) + dev
    dump_ticks: Dict[int, int] = field(default_factory=dict)   # tick -> dump ask offset below 1000
    mask_jumps: Set[int] = field(default_factory=set)

    @property
    def n(self):
        return len(self.tte)

    def mm_sigma(self):
        return self.pills_sigma * self.mm_vol_mult


def generate_day(spec: DaySpec, seed: str, n_ticks: int = TICKS_PER_DAY) -> DayPaths:
    rng = random.Random(f"{seed}|paths|{spec.label}")
    dt = SESSION_DAYS / n_ticks
    tte = [spec.tte_open - i * dt for i in range(n_ticks)]

    # COMET_PILLS: zero-drift GBM, sigma constant within the day (estimable from history).
    sigma = rng.uniform(0.15, 0.25)
    mm_mult = rng.choice((0.7, 1.4))
    s, pills = rng.uniform(1050, 1150), []
    sd = sigma * math.sqrt(dt)
    for _ in range(n_ticks):
        pills.append(s)
        s *= math.exp(sd * rng.gauss(0, 1) - 0.5 * sd * sd)

    # GAS_MASKS: the troll. Martingale log random walk, 4% per sqrt(100 ticks), plus fat jumps.
    m, masks, jumps = rng.uniform(450, 550), [], set()
    for i in range(n_ticks):
        masks.append(m)
        step = 0.004 * rng.gauss(0, 1)
        if rng.random() < 0.0015:
            step += rng.choice((-1, 1)) * rng.uniform(0.06, 0.15)
            jumps.add(i + 1)
        m *= math.exp(step - 0.5 * 0.004 ** 2)
        m = min(max(m, 120.0), 2500.0)

    # TELESCOPE_GLASS dumps: most follow a gas-mask jump by 1-3 ticks, a few are spontaneous.
    starts = [j + rng.randint(1, 3) for j in sorted(jumps) if rng.random() < 0.75]
    starts += [i for i in range(n_ticks) if rng.random() < 1 / 2000]
    dumps = {}
    for st in sorted(starts):
        depth = rng.randint(3, 8)
        for k in range(rng.randint(3, 6)):
            if 0 <= st + k < n_ticks:
                dumps[st + k] = depth

    # SURVIVAL_KIT mispricing: OU around 0 (half-life ~100 ticks, sd ~20) with occasional big slips.
    kappa, dev, kit_dev = 1 / 150, 0.0, []
    for _ in range(n_ticks):
        kit_dev.append(dev)
        dev += -kappa * dev + 2.3 * rng.gauss(0, 1)
        if rng.random() < 0.002:
            dev += rng.choice((-1, 1)) * rng.uniform(30, 60)

    glass = [float(GLASS_FAIR)] * n_ticks
    fair = {
        TELESCOPE_GLASS: glass,
        COMET_PILLS: pills,
        GAS_MASKS: masks,
        SURVIVAL_KIT: [glass[i] + pills[i] + masks[i] for i in range(n_ticks)],   # true value = sum of contents
        PILL_WARRANTS: [warrant_value(pills[i], tte[i], sigma) for i in range(n_ticks)],
        CALM_WEATHER_POLICIES: [policy_value(pills[i], tte[i], sigma) for i in range(n_ticks)],
    }
    return DayPaths(spec, fair, tte, sigma, mm_mult, kit_dev, dumps, jumps)
