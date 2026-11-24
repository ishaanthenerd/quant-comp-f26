# Quant Club Trading Competition: organiser project

| Path | What it is |
|---|---|
| `datamodel.py`, `protocol.py` | Order/TradingState types and the JSON wire format |
| `engine.py`, `bots.py`, `products.py`, `scenario.py` | Matching engine, the bot roster, the six products and the day schedule (all hidden from teams) |
| `datagen.py` | Writes IMC-format prices/trades CSVs (public days into `starter_kit/data/`, hidden days elsewhere) |
| `test_market.py` | Products, bots, matching priority, volume rules, CSVs, option settlement (runs anywhere) |
| `sandbox.py`, `sandbox_child.py` | Runs participant code in a separate, unprivileged, time-limited process |
| `run_job.py` | One grading job (the container entrypoint) |
| `worker.py` | SQLite job queue and worker loop (`--mode local` or `docker`) |
| `Dockerfile`, `DOCKER_SETUP.md` | Container image and how to deploy it |
| `starter_kit/` | What participants receive (also zipped as `starter_kit.zip`) |
| `test_sandbox.py` | Attack and correctness suite (run as root on Linux: `python3 test_sandbox.py`) |
| `test_docker.py` | Same idea against the real container (needs Docker) |
| `bench.py`, `timing_test.py`, `timing_stress/` | Engine benchmark, pandas timing |

Design doc: the "Quant Club Trading Competition: Design Doc" Claude Doc.

## The market (Halley's comet, Texas, May 1910)

**Units.** Prices are integer ticks of one cent; lore dollar amounts are x100: pills ~$11 -> ~1100,
warrant strike $12 -> 1200, policy pays $100 -> 10000 if pills < $8 -> 800. Telescope glass is fixed at 1000.

| Product | Fair value | Limit |
|---|---|---|
| `TELESCOPE_GLASS` | Fixed 1000. Comet-doubter dumps (offers 3-8 below 1000 for 3-6 ticks; preppers buy what is left at the end of each tick). most dumps (75% chance per jump) start 1-3 ticks after a gas-mask jump | 50 |
| `COMET_PILLS` | Zero-drift GBM, sigma constant within a day (0.15-0.25 per sqrt calendar day, from the seed) | 50 |
| `GAS_MASKS` | Log random walk, 0.4% per tick plus 6-15% jumps (~15 a day). An insider who sees 40 ticks ahead trades 6 or 7 lots | 50 |
| `SURVIVAL_KIT` | 1 glass + 1 pills + 1 mask. Market maker quotes sum + an OU error (sd ~25, half-life ~100 ticks, occasional 30-60 slips); marked at the plain sum | 25 |
| `PILL_WARRANTS` | Call on pills, strike 1200, expiry 9 am May 18; Black-Scholes with the true sigma | 100 |
| `CALM_WEATHER_POLICIES` | Cash-or-nothing put: 10000 if pills < 800 at expiry; 10000 * N(-d2), true sigma | 10 |

**Bots** (`bots.py`): market maker on every product (fair guess +- spread, 2 levels; options priced with 0.7x or 1.4x the
true sigma, chosen per day), filler (sloppy taker/rester around the TRUE value), three momentum townspeople (net aggressor
flow over 1-3 ticks), the gas-mask insider, the glass dumper and prepper. At most 3 price levels and 3 acting bots per side.
At equal price bot quotes fill before the participant's resting orders. Only the insider ever uses volume 6 or 7; other
bots use 8-25 (gas masks 5 and 8-20).

**Days.** 10,000 ticks (timestamps 0..999900), a 9 am-4 pm session. Public days -1 (May 15) and 0 (May 16) use a fixed seed
and are shipped as CSVs; hidden days 1 and 2 (independent May 15 and May 16 sessions) are seeded from `COMP_SECRET_SEED`.
A `slice` job (each website submission) runs public day 0; `full` (the final ranking) runs hidden days 1 and 2.

**Option settlement.** Sessions end before expiry, so end-of-day option positions are marked at the true-sigma model value
with the remaining time to expiry (1.71 days on May 16, 2.71 on May 15). Pills is a martingale under that sigma, so this
equals the expected payoff at expiry without the luck of one realised 0/10000 binary outcome.

```
python3 datagen.py public --out starter_kit/data
COMP_SECRET_SEED=... python3 datagen.py hidden --out /somewhere/private     # organisers only; refused into starter_kit/
python3 test_market.py && python3 bench.py
```

## Submission API

A standalone submission runner: the website (Next.js on Vercel) owns sign-in, teams and standings, and calls this
API with a signed JWT. The only auth here is that JWT check.

| Path | What it is |
|---|---|
| `app.py`, `common.py` | The FastAPI app (JWT check, uploads, limits, results, event stream) and shared helpers (event times, final run) |
| `manage.py` | Organiser CLI on the VM: job queue, re-run a job, final run |
| `API.md` | Endpoint reference for the website |
| `BACKEND_SETUP.md` | Putting the API on the VM, dry run, lock night |
| `test_api.py`, `test_jwt_api.py` | API test suites (`test_api.py` grades real jobs: run as root on Linux; `test_jwt_api.py` runs anywhere) |
| `requirements.txt` | Python packages for the API |
