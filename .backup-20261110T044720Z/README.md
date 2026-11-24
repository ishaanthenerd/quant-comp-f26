# Quant Club Trading Competition: organiser project

| Path | What it is |
|---|---|
| `datamodel.py`, `protocol.py` | Order/TradingState types and the JSON wire format |
| `engine.py`, `bots.py`, `scenario.py` | Matching engine with reactive bots; **placeholder** bots and scenario until assets exist |
| `sandbox.py`, `sandbox_child.py` | Runs participant code in a separate, unprivileged, time-limited process |
| `run_job.py` | One grading job (the container entrypoint) |
| `worker.py` | SQLite job queue and worker loop (`--mode local` or `docker`) |
| `ranking.py` | Final ranking rule (tiers: PnL >= 0, aborted, negative) |
| `Dockerfile`, `DOCKER_SETUP.md` | Container image and how to deploy it |
| `starter_kit/` | What participants receive (also zipped as `starter_kit.zip`) |
| `test_sandbox.py` | Attack and correctness suite (run as root on Linux: `python3 test_sandbox.py`) |
| `test_docker.py` | Same idea against the real container (needs Docker) |
| `test_ranking.py`, `bench.py`, `timing_test.py`, `timing_stress/` | Ranking test, engine benchmark, pandas timing |

Design doc: the "Quant Club Trading Competition: Design Doc" Claude Doc.

Not built yet: assets and real bots, data generation, website, final-run script, deployment.

## Backend and website (added)

| Path | What it is |
|---|---|
| `app.py`, `common.py` | The FastAPI backend and shared helpers (logins, rules, final run, leaderboard) |
| `manage.py` | Organiser CLI: manage teams and members, final run, ranking, release results |
| `API.md` | Endpoint reference for the frontend (give this to your friend) |
| `BACKEND_SETUP.md` | Putting the API on the VM, dry run, lock night |
| `test_api.py` | API test suite (`python3 test_api.py`, run as root on Linux) |
| `requirements.txt` | Python packages for the API |
