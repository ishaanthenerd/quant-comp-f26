# Quant Club Trading Competition: Starter Kit

You write one Python file with a `Trader` class. The server calls `Trader.run(state)` every tick,
you return orders, and your profit and loss (PnL) decides the ranking.

> Items marked **[TBD]** are filled in by the organisers before launch (Nov 10, 8pm Central).

## Quick start

```
pip install numpy pandas
python run_local.py examples/market_maker.py           # run on the practice market
python run_local.py my_trader.py --segments 2          # just 2 practice days (faster)
python run_local.py my_trader.py --sandbox             # same isolated process model as the server (Linux/macOS)
```

Start from `trader_template.py` or an example in `examples/`. Run with `--sandbox` before every
submission: it reproduces the server's import and time-limit behaviour.

## The practice market is not the real market

`scenario.py` here uses placeholder products (ALPHA to DELTA), simple price processes and simple bots.
The real competition has different products, different price behaviour and different bots, on data you
have never seen. Use the practice market to check that your code runs and is fast enough, not to tune parameters.
Strategies that only work on the practice market will not work on the real one.

## What you write

```python
from datamodel import Order

class Trader:
    def run(self, state):
        orders = {"SYMBOL": [Order("SYMBOL", price, quantity)]}   # quantity > 0 buys, < 0 sells; ints only
        return orders, 0, "my saved state"                        # (orders, conversions, trader_data)
```

`state` (a `TradingState`) contains:

| Field | Meaning |
|---|---|
| `state.timestamp` | Current tick time |
| `state.order_depths[sym]` | Bots' quotes. `.buy_orders = {price: +vol}`, `.sell_orders = {price: -vol}` (sell volumes are negative). Helpers: `.best_bid()`, `.best_ask()`, `.mid_price()` |
| `state.position[sym]` | Your current position |
| `state.position_limits[sym]` | Your limit for that product |
| `state.own_trades[sym]` | Your fills since last tick |
| `state.market_trades[sym]` | Other trades since last tick |
| `state.trader_data` | The string you returned last tick |
| `state.observations.plain_values` | Extra signals, if a product has any |

## How trading works

- Your orders trade immediately against the bots' quotes at the **bots' prices** (best price first).
- Whatever does not fill rests for the rest of that tick, where bots may trade against it, and is then cancelled.
  Orders do not carry over to the next tick.
- **Position limits:** if your buys (or sells) in a product could take you past the limit, **all** your orders
  in that product are rejected for that tick. Size your orders to `limit - position` (see `examples/market_maker.py`).
- **Bots react to you.** Trading a lot against a bot can move its quotes. Large aggressive orders cost more than they look.

## Saving state between ticks

You can keep variables on `self`, but if your process is restarted (for example after a slow tick), **only
`trader_data` survives**. Put anything you cannot afford to lose in `trader_data` (up to 50,000 characters;
`json.dumps` / `json.loads` work well). Each practice day or scoring segment starts with a fresh `Trader` and empty `trader_data`.

## Rules and limits

| | |
|---|---|
| File | One `.py` file, max 100 KB |
| Time per tick | 100 ms (the first call gets 2 s to allow for imports). A slow tick drops your orders for that tick |
| Too slow too often | More than 25 slow ticks in a segment aborts the run |
| Allowed imports | `datamodel`, `typing`, `math`, `statistics`, `collections`, `json`, `numpy`, `pandas`, `itertools`, `functools`, `heapq`, `copy`, `dataclasses` |
| Output | Your `print` output is shown to you in the results, capped in size |
| Errors | An exception in `run()` drops that tick's orders, and you see a traceback of your own code. The run continues |
| Network, files | Not available |

## Scoring

- Your PnL is cash plus the value of your open position, marked to the true value of each product at the end.
- **Submissions:** you can submit often. Each submission is graded on a **sample** of the hidden data and you see your
  own results only (**[TBD]**: limit of 15 submissions per day, 5-minute cooldown). The sample is for checking that your code works, not for tuning.
- **Final ranking:** your **last** submission before the lock (**Nov 16, 8pm Central**) is run on the **full** hidden data.
  **It counts even if it is broken**, so check that your final upload ran properly before the lock.
  Ranking order: runs with PnL of zero or more, by PnL; then runs that aborted or failed to load (scored PnL 0, not disqualified);
  then teams that never submitted (PnL 0); then runs with negative PnL, by PnL. Ties on PnL are broken by lower maximum drawdown.
- Results are announced the night of Nov 17.

## Submitting

**[TBD]** website address. Sign in with your Microsoft account, then either **create a team** (you get a join code to share with teammates) or **join one with a code**.
Teams can have up to 5 members who share the team's submissions and limits. Sign-up closes when submissions lock. Upload your single `.py` file; you will see a status
(queued, running, done, error), your results and your logs.

## Files in this kit

| File | Purpose |
|---|---|
| `trader_template.py` | Blank starting point with comments |
| `examples/market_maker.py` | Passive market maker with inventory skew |
| `examples/pandas_zscore.py` | Shows pandas inside `run()` and how to keep it fast |
| `run_local.py` | Runs your trader on the practice market |
| `datamodel.py` | `Order`, `TradingState`, `OrderDepth` and friends |
| `engine.py`, `bots.py`, `scenario.py` | The practice market (you can read them) |
| `sandbox.py`, `sandbox_child.py`, `protocol.py` | The isolated-process runner used by `--sandbox` |
