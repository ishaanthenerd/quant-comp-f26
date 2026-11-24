# Quant Club Trading Competition: organiser project

| Path | What it is |
|---|---|
| `datamodel.py`, `protocol.py` | Order/TradingState types and the JSON wire format |
| `engine.py`, `bots.py`, `scenario.py` | Matching engine with reactive bots; **placeholder** bots and scenario until assets exist |
| `sandbox.py`, `sandbox_child.py` | Runs participant code in a separate, unprivileged, time-limited process |
| `run_job.py` | One grading job (the container entrypoint) |
| `worker.py` | SQLite job queue and worker loop (`--mode local` or `docker`) |
| `Dockerfile`, `DOCKER_SETUP.md` | Container image and how to deploy it |
| `starter_kit/` | What participants receive (also zipped as `starter_kit.zip`) |
| `test_sandbox.py` | Attack and correctness suite (run as root on Linux: `python3 test_sandbox.py`) |
| `test_docker.py` | Same idea against the real container (needs Docker) |
| `bench.py`, `timing_test.py`, `timing_stress/` | Engine benchmark, pandas timing |

Design doc: the "Quant Club Trading Competition: Design Doc" Claude Doc.

Not built yet: assets and real bots, data generation.

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
