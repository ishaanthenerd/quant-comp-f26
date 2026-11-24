"""Products, bots, matching rules, CSV export and option settlement:  python3 test_market.py
Runs anywhere (no Docker, no root). Exit code 0 when everything passes."""
import csv
import math
import os
import random
import statistics as st
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.pop("COMP_SECRET_SEED", None)

import datagen
import products as P
import scenario
from bots import Bot, Ctx, Insider, Momentum
from datamodel import Order
from engine import Engine, Recorder

ok = True


def check(cond, msg, detail=""):
    global ok
    print(("PASS  " if cond else "FAIL  ") + msg + (f"   [{detail}]" if (detail and not cond) else ""))
    ok &= bool(cond)


class Idle:
    def run(self, s):
        return {}, 0, ""


def recorded(spec, n=P.TICKS_PER_DAY):
    eng = scenario.build_day(spec, n)
    rec = Recorder()
    eng.run(Idle(), recorder=rec)
    return eng, rec


PUB = {d.label: d for d in P.PUBLIC_DAYS}
runs = {d.label: recorded(d) for d in P.PUBLIC_DAYS}

# ---- products and limits -----------------------------------------------------------
check(scenario.PRODUCTS == ["TELESCOPE_GLASS", "COMET_PILLS", "GAS_MASKS", "SURVIVAL_KIT", "PILL_WARRANTS",
                            "CALM_WEATHER_POLICIES"], "the six tickers, exactly")
check(set(scenario.LIMITS) == set(scenario.PRODUCTS) and all(v > 0 for v in scenario.LIMITS.values()),
      "every product has a position limit")


class Grab:
    def __init__(self): self.state = None
    def run(self, s):
        self.state = self.state or s
        return {}, 0, ""


g = Grab()
scenario.build_day(PUB[0], 5).run(g)
check(g.state.position_limits == scenario.LIMITS, "limits reach the trader via state.position_limits")


class Overreach:
    def run(self, s):
        return {P.CALM_WEATHER_POLICIES: [Order(P.CALM_WEATHER_POLICIES, 10**6, scenario.LIMITS[P.CALM_WEATHER_POLICIES] + 1)]}, 0, ""


r = scenario.build_day(PUB[0], 50).run(Overreach())
check(r.limit_violations == 50 and r.final_positions[P.CALM_WEATHER_POLICIES] == 0, "orders past the limit are all rejected")

# ---- option maths and settlement -----------------------------------------------------
K, B = P.WARRANT_STRIKE, P.POLICY_THRESHOLD
check(P.bs_call(1300, K, 0, 0.2) == 100 and P.bs_call(1100, K, 0, 0.2) == 0 and P.bs_call(K, K, 0, 0.2) == 0,
      "warrant at expiry = max(S - 1200, 0), zero exactly at the strike")
check(P.binary_put(799.99, B, 0, 0.2) == 10000 and P.binary_put(800, B, 0, 0.2) == 0 and P.binary_put(800.01, B, 0, 0.2) == 0,
      "policy at expiry pays 10000 only strictly below 800 (exactly 800 pays 0)")
check(P.settle_at_expiry(799) == {P.PILL_WARRANTS: 0.0, P.CALM_WEATHER_POLICIES: 10000}
      and P.settle_at_expiry(1250) == {P.PILL_WARRANTS: 50.0, P.CALM_WEATHER_POLICIES: 0.0}, "settle_at_expiry intrinsic payoffs")
check(abs(P.bs_call(1300, K, 1e-9, 0.2) - 100) < 1e-3 and P.bs_call(1100, K, 1e-9, 0.2) < 1e-6,
      "warrant value converges to intrinsic as time to expiry -> 0")
check(P.binary_put(799, B, 1e-9, 0.2) > 9999 and P.binary_put(801, B, 1e-9, 0.2) < 1,
      "policy value converges to the step payoff as time to expiry -> 0")
at = P.binary_put(800, B, 1e-6, 0.2)
check(abs(at - 5000) < 5, "policy exactly at the threshold just before expiry is worth ~half the payout", at)
atm = P.bs_call(K, K, 2.0, 0.2)
check(abs(atm - K * (2 * P.norm_cdf(0.1 * math.sqrt(2.0)) - 1)) < 1e-6, "at-the-money call matches the closed form")
mono = all(P.bs_call(S, K, 1.5, 0.2) <= P.bs_call(S + 1, K, 1.5, 0.2) for S in range(900, 1500, 7))
mono &= all(P.binary_put(S, B, 1.5, 0.2) >= P.binary_put(S + 1, B, 1.5, 0.2) for S in range(600, 1200, 7))
check(mono, "warrant increases and policy decreases in the pills price")
check(all(0 <= P.binary_put(S, B, T, 0.2) <= 10000 and P.bs_call(S, K, T, 0.2) >= max(S - K, 0) - 1e-9
          for S in (500, 799, 800, 801, 1199, 1200, 1201, 2000) for T in (0, 0.01, 1, 3)), "option values within bounds")

# The mark (true-sigma model value at the close) equals the expected payoff at expiry (pills is a martingale GBM).
rng = random.Random(7)
eng0, _ = runs[0]
d0 = eng0.day
S0, T0, sig = d0.fair[P.COMET_PILLS][-1], d0.tte[-1], d0.pills_sigma
for S_start in (S0, 820.0, 1200.0):
    pays_c, pays_b = [], []
    for _ in range(40000):
        z = rng.gauss(0, 1)
        S = S_start * math.exp(sig * math.sqrt(T0) * z - 0.5 * sig * sig * T0)
        pays_c.append(max(S - K, 0)); pays_b.append(10000 if S < B else 0)
    for name, pays, mark in (("warrant", pays_c, P.warrant_value(S_start, T0, sig)),
                             ("policy", pays_b, P.policy_value(S_start, T0, sig))):
        se = st.pstdev(pays) / math.sqrt(len(pays))
        check(abs(st.mean(pays) - mark) < 4 * se + 1e-6,
              f"{name} mark at S={S_start:.0f} = Monte Carlo expected payoff at expiry", f"{st.mean(pays):.2f} vs {mark:.2f}")

check(all(abs(d0.fair[P.PILL_WARRANTS][i] - P.warrant_value(d0.fair[P.COMET_PILLS][i], d0.tte[i], d0.pills_sigma)) < 1e-9
          and abs(d0.fair[P.CALM_WEATHER_POLICIES][i] - P.policy_value(d0.fair[P.COMET_PILLS][i], d0.tte[i], d0.pills_sigma)) < 1e-9
          for i in range(0, d0.n, 97)), "option fair values are the TRUE-sigma model values")
check(abs(d0.tte[0] - 2.0) < 1e-12 and abs(d0.tte[-1] - (2.0 - P.SESSION_DAYS * (d0.n - 1) / d0.n)) < 1e-12 and d0.tte[-1] > 1.7,
      "May 16 session: 2 days to expiry at the open, ~1.71 at the close (ends before expiry)")


class BuyOnceHold:
    def __init__(self, p, q): self.p, self.q, self.done, self.paid = p, q, False, 0
    def run(self, s):
        if self.done:
            return {}, 0, ""
        self.done = True
        od = s.order_depths[self.p]
        px = od.best_ask() if self.q > 0 else od.best_bid()
        self.px = px
        return {self.p: [Order(self.p, px, self.q)]}, 0, ""


for p, q in ((P.PILL_WARRANTS, 5), (P.CALM_WEATHER_POLICIES, -3)):
    eng = scenario.build_day(PUB[0], 2000)
    t = BuyOnceHold(p, q)
    res = eng.run(t)
    expect = -q * t.px + q * eng.day.fair[p][-1]
    check(res.final_positions[p] == q and abs(res.pnl_by_product[p] - expect) < 1e-6 and abs(res.final_pnl - expect) < 1e-6,
          f"end-of-run {p} position is marked at the true-sigma model value", f"{res.pnl_by_product[p]} vs {expect}")

# ---- the options market maker uses the wrong volatility --------------------------------
for lbl, (eng, rec) in runs.items():
    d = eng.day
    for p in (P.PILL_WARRANTS, P.CALM_WEATHER_POLICIES):
        diffs = [(b[0][0] + a[0][0]) / 2 - d.fair[p][ts // 100] for ts, pp, b, a in rec.books if pp == p and b and a]
        sign = 1 if d.mm_vol_mult > 1 else -1
        hs = 2 if p == P.PILL_WARRANTS else 15
        check(sign * st.mean(diffs) > hs, f"day {lbl} {p}: MM mid is systematically off the true value "
              f"(vol x{d.mm_vol_mult}, mean error {st.mean(diffs):+.1f})")
mults = {P.generate_day(P.DaySpec(9, "x", 2.0), f"s{k}", 50).mm_vol_mult for k in range(40)}
check(mults == {0.7, 1.4}, "MM vol multiplier is 0.7 or 1.4, chosen per day from the seed")

# ---- pills: constant sigma within a day, estimable from history -------------------------
for lbl, (eng, _) in runs.items():
    d = eng.day
    x = d.fair[P.COMET_PILLS]
    rets = [math.log(x[i] / x[i - 1]) for i in range(1, len(x))]
    dt = P.SESSION_DAYS / d.n
    halves = [math.sqrt(st.pvariance(h) / dt) for h in (rets[:len(rets) // 2], rets[len(rets) // 2:])]
    check(all(abs(h / d.pills_sigma - 1) < 0.05 for h in halves), f"day {lbl}: realised pills vol (both halves) = the day's sigma",
          f"{halves} vs {d.pills_sigma}")

# ---- volumes: only the insider ever trades 6 or 7 --------------------------------------
for lbl, (eng, rec) in runs.items():
    bad_book = [(ts, p, v) for ts, p, b, a in rec.books for _, v in b + a if v in (6, 7)]
    check(not bad_book, f"day {lbl}: no 6 or 7 volume anywhere in the book", bad_book[:3])
    bad, ins = [], 0
    for (ts, p, px, q), (taker, maker, side) in zip(rec.trades, rec.trade_bots):
        insider = taker == "insider"
        ins += insider
        if insider != (q in (6, 7)) or (insider and p != P.GAS_MASKS) or not (insider or P.volume_allowed(p, q)):
            bad.append((ts, p, q, taker, maker))
    check(not bad and ins > 30, f"day {lbl}: tape volumes are 6/7 iff the insider traded ({ins} insider trades); "
          "other bots use 8-25 (GAS_MASKS 5, 8-20)", bad[:3])
    mask_q = {q for (ts, p, px, q), (tk, _, _) in zip(rec.trades, rec.trade_bots) if p == P.GAS_MASKS and tk != "insider"}
    check(mask_q and min(mask_q) >= 5 and max(mask_q) <= 20 and not mask_q & {6, 7}, f"day {lbl}: gas-mask non-insider volumes in 5..20")


class Nibbler:
    """Takes odd amounts out of every bot level so bots are left with awkward remnants."""
    def run(self, s):
        out = {}
        for p, od in s.order_depths.items():
            pos, lim = s.position.get(p, 0), s.position_limits[p]
            if od.best_ask() is not None and pos < lim - 3 and s.timestamp % 300 == 0:
                out[p] = [Order(p, od.best_ask(), 3)]
            elif od.best_bid() is not None and pos > -lim + 3 and s.timestamp % 300 == 100:
                out[p] = [Order(p, od.best_bid(), -2)]
            elif od.best_bid() is not None and od.best_ask() is not None:
                out[p] = [Order(p, od.best_bid() + 1, 1), Order(p, od.best_ask() - 1, -1)] if od.best_ask() - od.best_bid() > 2 else []
        return out, 0, ""


eng = scenario.build_day(PUB[0], 3000)
rec = Recorder()
eng.run(Nibbler(), recorder=rec)
bad = [(p, q, tk) for (ts, p, px, q), (tk, mk, sd) in zip(rec.trades, rec.trade_bots)
       if (tk == "insider") != (q in (6, 7)) or not (tk == "insider" or P.volume_allowed(p, q))]
check(not bad, "with a participant nibbling the book, bot-vs-bot fills still never show 6/7 (except the insider)", bad[:3])

# ---- depth ------------------------------------------------------------------------------
for lbl, (eng, rec) in runs.items():
    deep = max(max(len(b), len(a)) for _, _, b, a in rec.books)
    check(deep <= 3, f"day {lbl}: at most 3 price levels per side (max seen {deep})")
    crossed = [(ts, p) for ts, p, b, a in rec.books if b and a and b[0][0] >= a[0][0]]
    check(not crossed, f"day {lbl}: the book is never crossed", crossed[:3])


# ---- matching priority: market maker before the participant at equal price -----------
class FixedMM(Bot):
    name, is_mm = "market_maker", True
    def quotes(self, ctx):
        return [(997, 10)], [(1003, 10)]


class StubFiller(Bot):
    name = "filler"
    def __init__(self, product, rng, qty): super().__init__(product, rng); self.qty = qty
    def takes(self, ctx):
        return [("buy", 1010, self.qty)] if ctx.tick == 0 else []


def priority_run(player_px, qty):
    spec = P.DaySpec(5, "test", 2.0)
    day = P.DayPaths(spec, {P.TELESCOPE_GLASS: [1000.0, 1000.0]}, [2.0, 1.99], 0.2, 1.0, [0.0, 0.0])
    fac = lambda seed: {P.TELESCOPE_GLASS: [FixedMM(P.TELESCOPE_GLASS, random.Random(1)),
                                            StubFiller(P.TELESCOPE_GLASS, random.Random(2), qty)]}
    eng = Engine([P.TELESCOPE_GLASS], {P.TELESCOPE_GLASS: 50}, day, fac)
    seen = {}

    class T:
        def run(self, s):
            if s.timestamp == 0:
                return {P.TELESCOPE_GLASS: [Order(P.TELESCOPE_GLASS, player_px, -10)]}, 0, ""
            seen["own"], seen["mkt"] = s.own_trades.get(P.TELESCOPE_GLASS, []), s.market_trades.get(P.TELESCOPE_GLASS, [])
            return {}, 0, ""
    res = eng.run(T())
    return res.final_positions[P.TELESCOPE_GLASS], seen


pos, seen = priority_run(1003, 10)
check(pos == 0 and [t.quantity for t in seen["mkt"]] == [10], "filler buying 10 at the MM's ask: the MM fills first, the participant at the same price gets nothing")
pos, seen = priority_run(1003, 15)
check(pos == -5 and [(t.price, t.quantity) for t in seen["own"]] == [(1003, 5)], "filler buying 15: MM's 10 first, then 5 from the participant")
pos, seen = priority_run(1002, 10)
check(pos == -10 and not seen["mkt"], "a participant who improves the price is filled before the MM")
check(all(t.buyer is None and t.seller is None for t in seen["own"] + seen["mkt"] if t.buyer != "SUBMISSION" and t.seller != "SUBMISSION"),
      "market trades carry no bot names (the insider is identifiable only by volume)")

# ---- momentum and insider behaviour --------------------------------------------------------
day = runs[0][0].day
m = Momentum(P.COMET_PILLS, random.Random(3), lookback=2, threshold=20, prob=1.0, cooldown=5)
c = lambda flow: Ctx(10, 1000, P.COMET_PILLS, 1100.0, day, flow, best_bid=1098, best_ask=1102)
m.prepare(c([])); up = m.takes(c([0, 12, 12]))
m2 = Momentum(P.COMET_PILLS, random.Random(3), lookback=2, threshold=20, prob=1.0, cooldown=5)
m2.prepare(c([])); down = m2.takes(c([-15, -10]))
m3 = Momentum(P.COMET_PILLS, random.Random(3), lookback=2, threshold=20, prob=1.0, cooldown=5)
m3.prepare(c([])); quiet = m3.takes(c([30, 0, 5]))
check(up and up[0][0] == "buy" and down and down[0][0] == "sell" and not quiet,
      "momentum bots buy after net buying over their lookback, sell after net selling, ignore older flow")
check(up and up[0][2] in P.bot_volumes(P.COMET_PILLS), "momentum volumes from the allowed set")

for lbl, (eng, rec) in runs.items():
    path = eng.day.fair[P.GAS_MASKS]
    agree = [((1 if side == "buy" else -1) * (path[min(ts // 100 + 40, len(path) - 1)] - path[ts // 100]))
             for (ts, p, px, q), (tk, mk, side) in zip(rec.trades, rec.trade_bots) if tk == "insider"]
    hit = sum(a > 0 for a in agree) / len(agree)
    check(hit > 0.9 and st.mean(agree) > 10, f"day {lbl}: insider trades ahead of the move ({hit:.0%} right, mean {st.mean(agree):.1f} ticks)")
    fwd = [((1 if side == "buy" else -1) * (path[min(ts // 100 + 31, len(path) - 1)] - path[min(ts // 100 + 1, len(path) - 1)]))
           for (ts, p, px, q), (tk, mk, side) in zip(rec.trades, rec.trade_bots) if p == P.GAS_MASKS and q in (6, 7)]
    check(st.mean(fwd) > 5, f"day {lbl}: following a 6/7 trade from the NEXT tick still pays (mean {st.mean(fwd):.1f} ticks over 30)")

# ---- glass dumps, kit mispricing -----------------------------------------------------------
for lbl, (eng, rec) in runs.items():
    d = eng.day
    starts = [t for t in d.dump_ticks if t - 1 not in d.dump_ticks]
    after_jump = sum(any(t - k in d.mask_jumps for k in (1, 2, 3)) for t in starts)
    check(starts and after_jump / len(starts) > 0.5, f"day {lbl}: most glass dumps follow a gas-mask jump ({after_jump}/{len(starts)})")
    glass = [(ts, a) for ts, p, b, a in rec.books if p == P.TELESCOPE_GLASS]
    dumped = [ts // 100 for ts, a in glass if any(px <= 1000 - 3 and v >= 12 for px, v in a)]
    check(set(d.dump_ticks) <= set(dumped), f"day {lbl}: every dump tick shows glass offered 3+ below 1000")
    left = [ts for (ts, p, px, q), (tk, mk, sd) in zip(rec.trades, rec.trade_bots) if tk == "prepper"]
    check(len(left) > 0.5 * len(d.dump_ticks), f"day {lbl}: preppers buy up what the dumper offers")
    check(all(x == 1000 for x in d.fair[P.TELESCOPE_GLASS]), f"day {lbl}: glass fair value is fixed at 1000")
    dev = d.kit_dev
    lag = 300
    mu = st.mean(dev)
    ac = sum((dev[i] - mu) * (dev[i + lag] - mu) for i in range(len(dev) - lag)) / sum((x - mu) ** 2 for x in dev)
    check(st.pstdev(dev) > 10 and ac < 0.4, f"day {lbl}: kit mispricing is material (sd {st.pstdev(dev):.0f}) and mean-reverts (autocorr@300 {ac:.2f})")
    kit = {ts // 100: (b[0][0] + a[0][0]) / 2 for ts, p, b, a in rec.books if p == P.SURVIVAL_KIT and b and a}
    err = [kit[i] - sum(d.fair[c][i] for c in P.BASKET) - dev[i] for i in kit]
    check(abs(st.mean(err)) < 1 and st.mean(map(abs, err)) < 1.5, f"day {lbl}: kit quotes centre on sum(components) + the merchant's error")
    check(all(abs(d.fair[P.SURVIVAL_KIT][i] - sum(d.fair[c][i] for c in P.BASKET)) < 1e-9 for i in range(0, d.n, 101)),
          f"day {lbl}: the kit is marked at the sum of its contents")
    mk = d.fair[P.GAS_MASKS]
    r = [math.log(mk[i] / mk[i - 1]) for i in range(1, len(mk))]
    check(st.pstdev(r) > 3 * st.pstdev([math.log(d.fair[P.COMET_PILLS][i] / d.fair[P.COMET_PILLS][i - 1]) for i in range(1, d.n)]),
          f"day {lbl}: gas masks are far more volatile than pills")

# ---- seeds and CSVs ----------------------------------------------------------------------------
a1 = P.generate_day(P.HIDDEN_DAYS[0], "secret-A", 200).fair[P.COMET_PILLS]
a2 = P.generate_day(P.HIDDEN_DAYS[0], "secret-A", 200).fair[P.COMET_PILLS]
b1 = P.generate_day(P.HIDDEN_DAYS[0], "secret-B", 200).fair[P.COMET_PILLS]
check(a1 == a2 and a1 != b1, "generation is deterministic in the seed")
os.environ["COMP_SECRET_SEED"] = "secret-A"
hA = scenario.build_day(P.HIDDEN_DAYS[0], 200).day.fair[P.GAS_MASKS]
pA = scenario.build_day(P.PUBLIC_DAYS[0], 200).day.fair[P.GAS_MASKS]
os.environ["COMP_SECRET_SEED"] = "secret-B"
hB = scenario.build_day(P.HIDDEN_DAYS[0], 200).day.fair[P.GAS_MASKS]
pB = scenario.build_day(P.PUBLIC_DAYS[0], 200).day.fair[P.GAS_MASKS]
os.environ.pop("COMP_SECRET_SEED")
check(hA != hB and pA == pB, "hidden days depend on COMP_SECRET_SEED; public days do not")
check([lbl for lbl, _ in scenario.build("slice")] == [0] and [lbl for lbl, _ in scenario.build("full")] == [1, 2],
      "slice grades public day 0; full grades hidden days 1 and 2")

PRICES_HDR = ["day", "timestamp", "product", "bid_price_1", "bid_volume_1", "bid_price_2", "bid_volume_2", "bid_price_3",
              "bid_volume_3", "ask_price_1", "ask_volume_1", "ask_price_2", "ask_volume_2", "ask_price_3", "ask_volume_3",
              "mid_price", "profit_and_loss"]
TRADES_HDR = ["timestamp", "buyer", "seller", "symbol", "currency", "price", "quantity"]
ref = "/Users/davis/Library/asdf/GitHub/IMC-Prosperity-4/backtester/prosperity4bt/resources/round1"
if os.path.isdir(ref):
    check(open(os.path.join(ref, "prices_round_1_day_-1.csv")).readline().strip() == datagen.PRICES_HEADER,
          "prices header is byte-identical to IMC's")
    check(open(os.path.join(ref, "trades_round_1_day_-1.csv")).readline().strip() == datagen.TRADES_HEADER,
          "trades header is byte-identical to IMC's")
check(datagen.PRICES_HEADER.split(";") == PRICES_HDR and datagen.TRADES_HEADER.split(";") == TRADES_HDR, "CSV headers match IMC's columns")

tmp = tempfile.mkdtemp(prefix="csvtest-")
pp, tp = datagen.write_day(PUB[0], tmp, 400)
with open(pp) as f:
    rows = list(csv.reader(f, delimiter=";"))
with open(tp) as f:
    trows = list(csv.reader(f, delimiter=";"))
check(rows[0] == PRICES_HDR and len(rows) == 1 + 400 * 6 and all(len(r) == 17 for r in rows), "prices CSV: header, 6 rows per tick, 17 columns")
check(trows[0] == TRADES_HDR and len(trows) > 1 and all(len(r) == 7 and r[1] == r[2] == "" and r[4] == P.CURRENCY for r in trows[1:]),
      "trades CSV: header, empty buyer/seller, currency column")
r1 = rows[1]
check(r1[0] == "0" and r1[1] == "0" and float(r1[15]) == (int(r1[3]) + int(r1[9])) / 2 and r1[16] == "0.0",
      "prices row: day, timestamp, mid = (best bid + best ask) / 2, profit_and_loss 0.0")

# the public CSVs in the starter kit are exactly what the grader simulates
kit_dir = os.path.join(HERE, "starter_kit", "data")
for d in P.PUBLIC_DAYS:
    for kind in ("prices", "trades"):
        path = os.path.join(kit_dir, f"{kind}_round_1_day_{d.label}.csv")
        check(os.path.exists(path), f"starter_kit/data has {os.path.basename(path)}")
regen = tempfile.mkdtemp(prefix="regen-")
for d in P.PUBLIC_DAYS:
    for path in datagen.write_day(d, regen):
        shipped = os.path.join(kit_dir, os.path.basename(path))
        check(os.path.exists(shipped) and open(path).read() == open(shipped).read(),
              f"{os.path.basename(path)} in the starter kit regenerates byte-for-byte")
hidden_in_kit = [f for f in os.listdir(kit_dir) if any(f"_day_{d.label}." in f for d in P.HIDDEN_DAYS)] if os.path.isdir(kit_dir) else []
check(not hidden_in_kit, "no hidden-day CSVs in the starter kit", hidden_in_kit)

env = {k: v for k, v in os.environ.items() if k != "COMP_SECRET_SEED"}
p1 = subprocess.run([sys.executable, os.path.join(HERE, "datagen.py"), "hidden", "--out", tmp + "/h", "--ticks", "10"],
                    capture_output=True, text=True, env=env)
p2 = subprocess.run([sys.executable, os.path.join(HERE, "datagen.py"), "hidden", "--out", os.path.join(kit_dir, "x"), "--ticks", "10"],
                    capture_output=True, text=True, env=dict(env, COMP_SECRET_SEED="s"))
check(p1.returncode != 0 and p2.returncode != 0 and not os.path.exists(os.path.join(kit_dir, "x")),
      "datagen refuses hidden days without COMP_SECRET_SEED or into starter_kit/")

# the tape a participant sees equals the CSV (do-nothing participant)
eng, rec = runs[0]
csv_trades = [(int(r[0]), r[3], int(float(r[5])), int(r[6])) for r in csv.reader(open(os.path.join(regen, "trades_round_1_day_0.csv")), delimiter=";")
              if r[0] != "timestamp"]
check(csv_trades == [(ts, p, px, q) for ts, p, px, q in rec.trades], "the trades CSV is the tape the grader produces")

print("\nALL PASSED" if ok else "\nSOME FAILED")
raise SystemExit(0 if ok else 1)
