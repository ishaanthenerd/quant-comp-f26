"""
run_local.py: backtest your trader by REPLAYING the public CSVs in data/.

    python3 run_local.py my_trader.py                  # day 0 (the day every website submission is graded on)
    python3 run_local.py my_trader.py --day -1         # the other practice day
    python3 run_local.py my_trader.py --day all        # both practice days, each with a fresh Trader
    python3 run_local.py my_trader.py --quiet          # summary only (no per-tick errors / prints)
    python3 run_local.py my_trader.py --sandbox        # run your file in the server's isolated process (Linux/macOS)
    python3 run_local.py my_trader.py --strict-resting # only count resting fills on trades strictly THROUGH your price

What it does, per timestamp (0, 100, ..., 999900):
  1. Builds a TradingState: order depths from the CSV bid/ask levels 1-3, your position, your own
     trades and the market trades from the previous timestamp, position limits, timestamp and the
     trader_data string you returned last tick.
  2. Calls Trader.run(state). Same rules as the server: price and quantity must be Python ints;
     if a product's buys (or sells) could take you past its limit, ALL of that product's orders are
     rejected for the tick; an exception or a slow tick (> 100 ms; 2 s for the first call) drops
     that tick's orders; trader_data over 50,000 characters is ignored.
  3. Fills your orders:
       * AGGRESSIVE (crossing the book): filled against the CSV book levels at that timestamp, at the
         book's prices, best price first, up to the displayed volume. This matches the server.
       * RESTING (the unfilled rest): filled against the market trades printed at the same timestamp
         (the trades you would see as state.market_trades on the next tick), at YOUR price, when the
         trade printed at or through your price (a buy fills on prints <= your bid, a sell on prints
         >= your ask), up to the printed volume. Each printed trade can fill you only once.
     This is an APPROXIMATION. On the server, bot takers trade against your resting orders directly,
     bot quotes at the same price fill before yours (so "at your price" fills are optimistic: use
     --strict-resting for the pessimistic version), and the bots react to your trading. The replay
     cannot know how the market would have changed because of you: the book and the tape are fixed.
  4. Marks your PnL to the CSV mid_price every tick (cash + position * mid). The server marks to each
     product's TRUE value instead; for PILL_WARRANTS and CALM_WEATHER_POLICIES that is the option
     model value (see README), so the local option PnL is only an approximation.

At the end of each day it prints PnL per product, the total and the maximum drawdown (all in cents).
"""
import argparse
import ast
import contextlib
import csv
import importlib.util
import io
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from datamodel import Listing, Observation, Order, OrderDepth, Trade, TradingState  # noqa: E402

PRODUCTS = ["TELESCOPE_GLASS", "COMET_PILLS", "GAS_MASKS", "SURVIVAL_KIT", "PILL_WARRANTS", "CALM_WEATHER_POLICIES"]
LIMITS = {"TELESCOPE_GLASS": 50, "COMET_PILLS": 50, "GAS_MASKS": 50, "SURVIVAL_KIT": 25,
          "PILL_WARRANTS": 100, "CALM_WEATHER_POLICIES": 10}
CURRENCY = "CENTS"
SUBMISSION = "SUBMISSION"
TICK_LIMIT, FIRST_CALL_LIMIT = 0.100, 2.0
MAX_TRADER_DATA = 50_000
ALLOWED_IMPORTS = frozenset({
    "datamodel", "typing", "math", "statistics", "collections", "json",
    "numpy", "pandas", "itertools", "functools", "heapq", "copy", "dataclasses",
})


# ---- data ---------------------------------------------------------------------------
def load_prices(path):
    """-> (sorted timestamps, {ts: {product: (bids [(px, vol)] best-first, asks [(px, vol)] best-first, mid)}})"""
    books = {}
    with open(path, newline="") as f:
        r = csv.reader(f, delimiter=";")
        next(r)
        for row in r:
            ts, prod = int(row[1]), row[2]
            bids = [(int(row[3 + 2 * k]), int(row[4 + 2 * k])) for k in range(3) if row[3 + 2 * k]]
            asks = [(int(row[9 + 2 * k]), int(row[10 + 2 * k])) for k in range(3) if row[9 + 2 * k]]
            mid = float(row[15]) if row[15] else 0.0
            books.setdefault(ts, {})[prod] = (bids, asks, mid)
    return sorted(books), books


def load_trades(path):
    """-> {ts: {product: [(price, qty)]}}"""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="") as f:
        r = csv.reader(f, delimiter=";")
        next(r)
        for row in r:
            out.setdefault(int(row[0]), {}).setdefault(row[3], []).append((int(float(row[5])), int(row[6])))
    return out


# ---- running the participant's code ------------------------------------------------
def check_imports(path):
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError as e:
        sys.exit(f"Syntax error in {path}: {e}")
    bad = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bad.add(("." * node.level + (node.module or "")).split(".")[0] or "relative")
    bad -= ALLOWED_IMPORTS
    if bad:
        print(f"WARNING: the server would REJECT these imports: {sorted(bad)}")
        print(f"         allowed: {sorted(ALLOWED_IMPORTS)}\n")


class InProcessTrader:
    """Calls your Trader directly (print() and debuggers work). Mirrors the server's per-tick rules,
    but not its memory limits or import allowlist: use --sandbox for that."""

    def __init__(self, path, quiet):
        spec = importlib.util.spec_from_file_location("submission", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.trader = mod.Trader()
        self.quiet = quiet
        self.timeouts = self.errors = self.crashes = self.restarts = self.invalid = 0
        self.tick_times, self.logs, self._shown, self._called = [], [], 0, False

    def run(self, state):
        empty = ({}, 0, state.trader_data)
        limit = TICK_LIMIT if self._called else FIRST_CALL_LIMIT
        self._called = True
        t0 = time.perf_counter()
        try:
            if self.quiet:
                with contextlib.redirect_stdout(io.StringIO()):
                    orders, _conv, td = self.trader.run(state)
            else:
                orders, _conv, td = self.trader.run(state)
            if not isinstance(td, str):
                raise TypeError("traderData must be a str")
            if len(td) > MAX_TRADER_DATA:
                raise ValueError(f"traderData too large (max {MAX_TRADER_DATA} chars)")
        except Exception:
            self.errors += 1
            if self._shown < 5 and not self.quiet:
                self._shown += 1
                print(f"[t={state.timestamp}] ERROR in run():\n{traceback.format_exc()}")
            return empty
        dt = time.perf_counter() - t0
        if dt > limit:
            self.timeouts += 1
            if self.timeouts <= 5 and not self.quiet:
                print(f"[t={state.timestamp}] time limit exceeded ({dt * 1000:.0f} ms > {limit * 1000:.0f} ms): orders dropped")
            return empty
        self.tick_times.append(dt)
        return orders, 0, td

    def start(self):
        pass

    def close(self):
        pass


# ---- one day -------------------------------------------------------------------------
def run_day(day, data_dir, make_trader, strict, quiet):
    prices = os.path.join(data_dir, f"prices_round_1_day_{day}.csv")
    if not os.path.exists(prices):
        sys.exit(f"Missing {prices}")
    stamps, books = load_prices(prices)
    tape = load_trades(os.path.join(data_dir, f"trades_round_1_day_{day}.csv"))

    products = [p for p in PRODUCTS if any(p in books[t] for t in stamps[:1])] or PRODUCTS
    listings = {p: Listing(p, p, CURRENCY) for p in products}
    pos = {p: 0 for p in products}
    cash = {p: 0.0 for p in products}
    last_mid = {p: 0.0 for p in products}
    trader_data, own_prev, mkt_prev = "", {}, {}
    series, violations, n_fills = [], 0, {"aggressive": 0, "resting": 0}

    trader = make_trader()
    trader.start()
    try:
        for ts in stamps:
            book = books[ts]
            depths = {}
            for p in products:
                bids, asks, mid = book.get(p, ([], [], 0.0))
                od = OrderDepth()
                for px, v in bids:
                    od.buy_orders[px] = od.buy_orders.get(px, 0) + v
                for px, v in asks:
                    od.sell_orders[px] = od.sell_orders.get(px, 0) - v
                depths[p] = od
                if mid:
                    last_mid[p] = mid

            state = TradingState(trader_data, ts, listings, depths, own_prev, mkt_prev,
                                 dict(pos), dict(LIMITS), Observation())
            result = trader.run(state)
            try:
                orders_by_sym, _conv, new_td = result
            except (TypeError, ValueError):
                orders_by_sym, new_td = {}, trader_data
            if isinstance(new_td, str) and len(new_td) <= MAX_TRADER_DATA:
                trader_data = new_td
            if not isinstance(orders_by_sym, dict):
                orders_by_sym = {}

            own_now = {}
            tape_now = tape.get(ts, {})
            for p in products:
                bids, asks, _ = book.get(p, ([], [], 0.0))
                orders = orders_by_sym.get(p) or []
                valid = [o for o in orders if isinstance(o, Order) and o.symbol == p
                         and type(o.price) is int and type(o.quantity) is int and o.quantity != 0]
                buys = sum(o.quantity for o in valid if o.quantity > 0)
                sells = sum(-o.quantity for o in valid if o.quantity < 0)
                if valid and (pos[p] + buys > LIMITS[p] or pos[p] - sells < -LIMITS[p]):
                    violations += 1
                    valid = []
                if not valid:
                    continue

                # aggressive part: against the displayed book, at the book's prices
                bl = [[px, v] for px, v in bids]
                al = [[px, v] for px, v in asks]
                rest_buys, rest_sells = [], []
                for o in valid:
                    qty, buy = abs(o.quantity), o.quantity > 0
                    for lvl in (al if buy else bl):
                        if qty == 0 or (lvl[0] > o.price if buy else lvl[0] < o.price):
                            break
                        f = min(qty, lvl[1])
                        if f <= 0:
                            continue
                        lvl[1] -= f
                        qty -= f
                        n_fills["aggressive"] += 1
                        if buy:
                            pos[p] += f; cash[p] -= f * lvl[0]
                            own_now.setdefault(p, []).append(Trade(p, lvl[0], f, SUBMISSION, None, ts))
                        else:
                            pos[p] -= f; cash[p] += f * lvl[0]
                            own_now.setdefault(p, []).append(Trade(p, lvl[0], f, None, SUBMISSION, ts))
                    if qty:
                        (rest_buys if buy else rest_sells).append([o.price, qty])

                # resting part: against this timestamp's printed market trades, at our price
                prints = tape_now.get(p)
                if not prints or not (rest_buys or rest_sells):
                    continue
                best_bid = bids[0][0] if bids else None
                best_ask = asks[0][0] if asks else None
                left = [[px, q] for px, q in prints]
                # a print below the best ask was a sell into the bids; above the best bid, a buy from the asks
                sell_prints = [t for t in left if best_ask is None or t[0] < best_ask]
                buy_prints = [t for t in left if best_bid is None or t[0] > best_bid]
                for mine, side_prints, buy in ((sorted(rest_buys, key=lambda o: -o[0]), sell_prints, True),
                                               (sorted(rest_sells, key=lambda o: o[0]), buy_prints, False)):
                    for o in mine:
                        for t in side_prints:
                            if o[1] == 0:
                                break
                            if t[1] <= 0:
                                continue
                            hit = (t[0] < o[0] if strict else t[0] <= o[0]) if buy else \
                                  (t[0] > o[0] if strict else t[0] >= o[0])
                            if not hit:
                                continue
                            f = min(o[1], t[1])
                            o[1] -= f
                            t[1] -= f
                            n_fills["resting"] += 1
                            if buy:
                                pos[p] += f; cash[p] -= f * o[0]
                                own_now.setdefault(p, []).append(Trade(p, o[0], f, SUBMISSION, None, ts))
                            else:
                                pos[p] -= f; cash[p] += f * o[0]
                                own_now.setdefault(p, []).append(Trade(p, o[0], f, None, SUBMISSION, ts))

            own_prev = own_now
            mkt_prev = {p: [Trade(p, px, q, None, None, ts) for px, q in lst] for p, lst in tape_now.items()}
            series.append(sum(cash[p] + pos[p] * last_mid[p] for p in products))
    finally:
        trader.close()

    by_prod = {p: cash[p] + pos[p] * last_mid[p] for p in products}
    peak, mdd = float("-inf"), 0.0
    for v in series:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return dict(pnl=sum(by_prod.values()), by_prod=by_prod, positions=dict(pos), series=series,
                max_dd=mdd, violations=violations, fills=n_fills, trader=trader, ticks=len(stamps))


def main():
    ap = argparse.ArgumentParser(description="Replay the public CSVs against your Trader.")
    ap.add_argument("trader", help="your submission file (defines class Trader)")
    ap.add_argument("--day", default="0", choices=["-1", "0", "all"],
                    help="practice day to replay (default 0, the day website submissions are graded on)")
    ap.add_argument("--data", default=os.path.join(HERE, "data"), help="directory with the CSVs (default: data/)")
    ap.add_argument("--quiet", action="store_true", help="only print the summary")
    ap.add_argument("--sandbox", action="store_true",
                    help="run your file in the server's isolated child process (import allowlist, time limits)")
    ap.add_argument("--strict-resting", action="store_true",
                    help="resting orders only fill on prints strictly through your price (pessimistic)")
    a = ap.parse_args()

    path = os.path.abspath(a.trader)
    if not os.path.exists(path):
        sys.exit(f"No such file: {path}")
    check_imports(path)
    days = [-1, 0] if a.day == "all" else [int(a.day)]

    if a.sandbox:
        from sandbox import RunAborted, SandboxedTrader, SubmissionError

        def make_trader():
            return SandboxedTrader(path, drop_privs=False)
    else:
        class RunAborted(BaseException):
            pass

        class SubmissionError(Exception):
            pass

        def make_trader():
            return InProcessTrader(path, a.quiet)

    t0 = time.time()
    results = []
    try:
        for d in days:
            t_day = time.time()
            r = run_day(d, a.data, make_trader, a.strict_resting, a.quiet)
            results.append((d, r))
            tr = r["trader"]
            if a.sandbox and tr.logs and not a.quiet:
                print("\n".join(tr.logs))
            print(f"\n=== day {d}: {r['ticks']} ticks in {time.time() - t_day:.1f}s ===")
            for p, v in r["by_prod"].items():
                print(f"  {p:<23} pnl {v:12.1f}   final position {r['positions'][p]:5d}")
            print(f"  {'TOTAL':<23} pnl {r['pnl']:12.1f}   max drawdown {r['max_dd']:.1f}")
            print(f"  fills: {r['fills']['aggressive']} aggressive, {r['fills']['resting']} resting   "
                  f"position-limit rejections: {r['violations']}   errors: {tr.errors}   timeouts: {tr.timeouts}"
                  + (f"   crashes: {tr.crashes}   invalid: {tr.invalid}" if a.sandbox else ""))
            if tr.tick_times:
                s = sorted(tr.tick_times)
                print(f"  tick time ms: mean {1000 * sum(s) / len(s):.2f}   p99 {1000 * s[int(.99 * (len(s) - 1))]:.2f}"
                      f"   max {1000 * s[-1]:.2f}   (server limit {TICK_LIMIT * 1000:.0f})")
    except SubmissionError as e:
        sys.exit(f"\nCould not load submission:\n{e}\n(On the server this scores PnL 0.)")
    except RunAborted as e:
        sys.exit(f"\nRun aborted: {e}\n(On the server this scores PnL 0.)")

    if len(results) > 1:
        offset, series = 0.0, []
        for _, r in results:
            series += [offset + v for v in r["series"]]
            offset += r["pnl"]
        peak, dd = float("-inf"), 0.0
        for v in series:
            peak = max(peak, v)
            dd = max(dd, peak - v)
        print(f"\n=== all days: total pnl {offset:.1f}   max drawdown {dd:.1f} ===")
    print(f"\nPnL is in cents, marked to the CSV mid (options: see README). elapsed {time.time() - t0:.1f}s")
    if any(r["violations"] for _, r in results):
        print("Tip: if your buys (or sells) in a product could breach its limit, ALL that product's orders are rejected that tick.")


if __name__ == "__main__":
    main()
