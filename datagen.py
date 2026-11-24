"""
datagen.py: write IMC Prosperity-format CSVs for the Halley's comet market.

    python3 datagen.py public --out starter_kit/data          # the 2 public days (fixed seed)
    COMP_SECRET_SEED=... python3 datagen.py hidden --out /root/hidden-data   # the 2 hidden days

For each day it writes prices_round_1_day_<d>.csv and trades_round_1_day_<d>.csv,
semicolon-separated with IMC's exact headers. The data is the market as it runs with
nobody else trading (a do-nothing participant), so it is exactly what the grader replays
before your orders change anything. Market trades have empty buyer/seller (as in IMC
round 1); profit_and_loss is 0.0.

Hidden days need COMP_SECRET_SEED and are refused into starter_kit/. Never commit or ship them.
"""
import argparse
import os
import sys

import products as P
import scenario
from engine import Recorder

PRICES_HEADER = ("day;timestamp;product;bid_price_1;bid_volume_1;bid_price_2;bid_volume_2;bid_price_3;"
                 "bid_volume_3;ask_price_1;ask_volume_1;ask_price_2;ask_volume_2;ask_price_3;ask_volume_3;"
                 "mid_price;profit_and_loss")
TRADES_HEADER = "timestamp;buyer;seller;symbol;currency;price;quantity"
ROUND = 1


class _Idle:
    def run(self, state):
        return {}, 0, ""


def _mid(bids, asks):
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2
    if bids:
        return float(bids[0][0])
    if asks:
        return float(asks[0][0])
    return 0.0


def record_day(spec, n_ticks=P.TICKS_PER_DAY):
    eng = scenario.build_day(spec, n_ticks)
    rec = Recorder()
    eng.run(_Idle(), recorder=rec)
    return rec


def write_day(spec, out_dir, n_ticks=P.TICKS_PER_DAY):
    rec = record_day(spec, n_ticks)
    os.makedirs(out_dir, exist_ok=True)
    pp = os.path.join(out_dir, f"prices_round_{ROUND}_day_{spec.label}.csv")
    tp = os.path.join(out_dir, f"trades_round_{ROUND}_day_{spec.label}.csv")
    with open(pp, "w", newline="\n") as f:
        f.write(PRICES_HEADER + "\n")
        for ts, prod, bids, asks in rec.books:
            cells = [str(spec.label), str(ts), prod]
            for side in (bids, asks):
                for k in range(3):
                    cells += [str(side[k][0]), str(side[k][1])] if k < len(side) else ["", ""]
            cells += [repr(float(_mid(bids, asks))), "0.0"]
            f.write(";".join(cells) + "\n")
    with open(tp, "w", newline="\n") as f:
        f.write(TRADES_HEADER + "\n")
        for ts, prod, price, qty in rec.trades:
            f.write(f"{ts};;;{prod};{P.CURRENCY};{float(price)!r};{qty}\n")
    return pp, tp


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("which", choices=["public", "hidden"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--ticks", type=int, default=P.TICKS_PER_DAY, help="ticks per day (tests only)")
    a = ap.parse_args()
    if a.which == "hidden":
        if not os.environ.get("COMP_SECRET_SEED"):
            sys.exit("hidden days need COMP_SECRET_SEED set (the same value the grader uses)")
        kit = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "starter_kit"))
        out = os.path.realpath(a.out)
        if out == kit or out.startswith(kit + os.sep):
            sys.exit("refusing to write hidden days into starter_kit/")
    days = P.PUBLIC_DAYS if a.which == "public" else P.HIDDEN_DAYS
    for d in days:
        for path in write_day(d, a.out, a.ticks):
            print("wrote", path)


if __name__ == "__main__":
    main()
