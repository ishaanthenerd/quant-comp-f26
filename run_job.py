"""
run_job.py: TRUSTED entry point for one grading job (this is what the container runs).

    python run_job.py --submission sub.py --kind slice|full --out result.json

Runs every scenario day (slice: the public sample day; full: the hidden days) against the submission via SandboxedTrader and
writes a result file containing ONLY what teams are allowed to see.
"""
import argparse
import json
import os
import signal
import sys
import time
import traceback

import scenario
from sandbox import RunAborted, SandboxedTrader, SubmissionError

# slice = 1 public day, full = 2 hidden days, 10,000 ticks each: about 120 ms per tick on average,
# so a run that stays inside the 100 ms per-tick limit is never stopped by the total limit.
TOTAL_TIMEOUT = {"slice": 1200, "full": 2400}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--submission", required=True)
    ap.add_argument("--kind", choices=["slice", "full"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-drop", action="store_true", help="dev only: don't drop to an unprivileged uid")
    a = ap.parse_args()

    t0 = time.time()
    res = {"status": "ok", "message": "", "kind": a.kind}
    counters = dict(timeouts=0, crashes=0, errors=0, restarts=0, invalid=0)
    logs, segments, series, offset, tick_times = [], [], [], 0.0, []
    totals_by_product, max_dd, ticks = {}, 0.0, 0

    def on_alarm(signum, frame):
        raise RunAborted("total run time limit exceeded")
    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(TOTAL_TIMEOUT[a.kind])

    try:
        for seg_id, eng in scenario.build(a.kind):
            st = SandboxedTrader(a.submission, drop_privs=not a.no_drop)
            try:
                st.start()
                r = eng.run(st)
            finally:
                st.close()
                for k in counters:
                    counters[k] += getattr(st, k)
                logs += st.logs
                tick_times += st.tick_times
            segments.append({"pnl": round(r.final_pnl, 2),
                             "positions": r.final_positions,
                             "limit_violations": r.limit_violations})
            for p, v in r.pnl_by_product.items():
                totals_by_product[p] = totals_by_product.get(p, 0.0) + v
            series += [offset + v for v in r.pnl_series]
            offset += r.final_pnl
            ticks += len(r.pnl_series)
            res.setdefault("limit_violations", 0)
            res["limit_violations"] += r.limit_violations
        peak = float("-inf")
        for v in series:
            peak = max(peak, v)
            max_dd = max(max_dd, peak - v)
        step = max(1, len(series) // 200)
        res.update(pnl=round(offset, 2),
                   pnl_by_product={k: round(v, 2) for k, v in totals_by_product.items()},
                   max_drawdown=round(max_dd, 2), segments=segments,
                   pnl_curve=[round(v, 1) for v in series[::step]], ticks=ticks)
    except SubmissionError as e:
        res.update(status="error", pnl=0.0, aborted=True, message=f"Could not load submission:\n{e}")
    except RunAborted as e:
        res.update(status="error", pnl=0.0, aborted=True, message=f"Run aborted: {e}")
    except Exception:
        traceback.print_exc(file=sys.stderr)       # admin-only; teams see a generic message
        res.update(status="error", internal=True,
                   message="Internal grading error. The organisers have been notified.")
    finally:
        signal.alarm(0)

    res.setdefault("aborted", False)
    if tick_times:
        tt = sorted(tick_times)
        res.update(mean_tick_ms=round(1000 * sum(tt) / len(tt), 2),
                   p99_tick_ms=round(1000 * tt[int(0.99 * (len(tt) - 1))], 2),
                   max_tick_ms=round(1000 * tt[-1], 2))
    res.update(counters)
    res["logs"] = "\n".join(logs)[:20_000]
    res["elapsed_s"] = round(time.time() - t0, 2)
    if a.out == "-":
        # Docker mode: the result goes to the container's stdout, which only this (trusted)
        # process holds. No writable mount exists for a participant process to tamper with.
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    else:
        tmp = a.out + ".tmp"
        with open(tmp, "w") as f:
            json.dump(res, f)
        os.replace(tmp, a.out)


if __name__ == "__main__":
    main()
