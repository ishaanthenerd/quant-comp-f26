"""
Run your trader on the practice market.

    python run_local.py my_trader.py                 # fast, in-process (works everywhere)
    python run_local.py my_trader.py --segments 2    # just the first 2 practice days
    python run_local.py my_trader.py --sandbox       # same isolated process model as the server (Linux/macOS)

In-process mode lets you use print() and a debugger. It checks the same 100 ms
per-tick limit and warns about imports the server would reject, but it does not
enforce memory limits or the import allowlist. Run with --sandbox before you
submit to see exactly what the server will see.
"""
import argparse
import importlib.util
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenario  # noqa: E402

TICK_LIMIT, FIRST_CALL_LIMIT = 0.100, 2.0


def check_imports(path):
    import ast
    from sandbox_child import ALLOWED_IMPORTS
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
    def __init__(self, path):
        spec = importlib.util.spec_from_file_location("submission", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.trader = mod.Trader()
        self.timeouts = self.errors = self.crashes = self.restarts = self.invalid = 0
        self.tick_times, self.logs, self._shown, self._called = [], [], 0, False

    def run(self, state):
        empty = ({}, 0, state.trader_data)
        limit = TICK_LIMIT if self._called else FIRST_CALL_LIMIT
        self._called = True
        t0 = time.perf_counter()
        try:
            orders, _conv, td = self.trader.run(state)
            if not isinstance(td, str):
                raise TypeError("traderData must be a str")
        except Exception:
            self.errors += 1
            if self._shown < 5:
                self._shown += 1
                print(f"[t={state.timestamp}] ERROR in run():\n{traceback.format_exc()}")
            return empty
        dt = time.perf_counter() - t0
        if dt > limit:
            self.timeouts += 1
            if self.timeouts <= 5:
                print(f"[t={state.timestamp}] time limit exceeded ({dt * 1000:.0f} ms > {limit * 1000:.0f} ms): orders dropped")
            return empty
        self.tick_times.append(dt)
        return orders, 0, td

    def close(self):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trader")
    ap.add_argument("--segments", type=int, default=scenario.N_SEGMENTS)
    ap.add_argument("--sandbox", action="store_true")
    a = ap.parse_args()
    path = os.path.abspath(a.trader)
    if not os.path.exists(path):
        sys.exit(f"No such file: {path}")
    check_imports(path)
    if a.sandbox:
        from sandbox import RunAborted, SandboxedTrader, SubmissionError
    t0 = time.time()
    tot, by_prod, ticks_all, viol, ex = 0.0, {}, [], 0, dict(timeouts=0, errors=0, crashes=0)
    offset, series = 0.0, []
    try:
        for seg, eng in scenario.build(a.segments):
            tr = SandboxedTrader(path) if a.sandbox else InProcessTrader(path)
            try:
                if a.sandbox:
                    tr.start()
                res = eng.run(tr)
            finally:
                tr.close()
            if a.sandbox and tr.logs:
                print("\n".join(tr.logs))
            for k in ex:
                ex[k] += getattr(tr, k)
            ticks_all += tr.tick_times
            viol += res.limit_violations
            tot += res.final_pnl
            for p, v in res.pnl_by_product.items():
                by_prod[p] = by_prod.get(p, 0.0) + v
            series += [offset + v for v in res.pnl_series]
            offset += res.final_pnl
            print(f"  day {seg}: pnl {res.final_pnl:10.1f}   positions {list(res.final_positions.values())}")
    except Exception as e:
        if a.sandbox and type(e).__name__ == "SubmissionError":
            sys.exit(f"\nCould not load submission:\n{e}\n(On the server this scores PnL 0 and ranks below all non-negative runs.)")
        raise
    except BaseException as e:
        if type(e).__name__ == "RunAborted":
            sys.exit(f"\nRun aborted: {e}\n(On the server this scores PnL 0 and ranks below all non-negative runs.)")
        raise
    peak, dd = float("-inf"), 0.0
    for v in series:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    print(f"\nTOTAL PnL {tot:.1f}   max drawdown {dd:.1f}")
    print("per product:", {k: round(v, 1) for k, v in by_prod.items()})
    print(f"position-limit violations: {viol}   errors: {ex['errors']}   timeouts: {ex['timeouts']}")
    if ticks_all:
        s = sorted(ticks_all)
        print(f"tick time ms: mean {1000 * sum(s) / len(s):.2f}   p99 {1000 * s[int(.99 * (len(s) - 1))]:.2f}   "
              f"max {1000 * s[-1]:.2f}   (limit {TICK_LIMIT * 1000:.0f})")
    print(f"elapsed {time.time() - t0:.1f}s")
    if viol:
        print("Tip: if buys/sells in a product could breach the limit, ALL that product's orders are rejected that tick.")


if __name__ == "__main__":
    main()
