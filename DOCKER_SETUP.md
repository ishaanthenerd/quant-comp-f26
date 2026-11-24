# Running the grader in Docker

The worker runs each submission inside a throwaway container with no network, a read-only
filesystem, dropped capabilities, and memory/CPU/process caps. The container's entrypoint
(`run_job.py`) is the trusted engine; it starts the participant's code as a separate unprivileged
process (uid 65534) inside the same container, and prints the result as one JSON line on stdout.

## What you need

- A Linux server (Ubuntu 22.04 or 24.04 is a safe choice), with root or sudo.
- Docker Engine: https://docs.docker.com/engine/install/ubuntu/
- Python 3.10+ on the host. The worker and test harness use only the standard library.
- This project folder on the server (`scp -r quant_comp user@server:~`).

## 1. Build the image

```
cd ~/quant_comp
docker build -t quant-comp-runner:v1 .
export COMP_IMAGE=quant-comp-runner:v1     # tells worker.py which image to run
```

Tag every rebuild with a new version (`v2`, `v3`...). Never change the image in the middle of the
event unless you announce it and re-run stored submissions, otherwise teams are graded by different engines.

## 2. Create the hidden seed

```
sudo install -m 600 /dev/null /etc/quant-comp.env
echo "COMP_SECRET_SEED=$(openssl rand -hex 32)" | sudo tee -a /etc/quant-comp.env >/dev/null
echo "COMP_IMAGE=quant-comp-runner:v1"           | sudo tee -a /etc/quant-comp.env >/dev/null
```

- The seed generates the hidden data and picks the slice. **Back it up somewhere offline.**
  If you lose it, or change it after launch, every stored score becomes incomparable and the final run cannot be reproduced.
- Never put it in the starter kit, the website code, or a git repo.
- The worker refuses to start Docker jobs if it is unset or empty.

## 3. Run the smoke test (do this before anything else)

```
python3 test_docker.py             # security and correctness checks
python3 test_docker.py --timing    # also times the pandas stress traders under the container's 1-CPU cap
```

Every line should say `PASS`. The test deliberately defeats the Python import allowlist, then confirms the participant process still
cannot reach the network, read the engine's environment or source, write to the filesystem, kill the engine or become root.
Read the `--timing` numbers: they are measured under the container's `--cpus 1` cap, which is slower than the numbers from the dev machine.

## 4. Start the workers

```
sudo mkdir -p /var/lib/quant-comp && sudo chown $USER /var/lib/quant-comp
python3 worker.py init --db /var/lib/quant-comp/jobs.db

# try it once in the foreground:
set -a; . /etc/quant-comp.env; set +a
python3 worker.py enqueue --db /var/lib/quant-comp/jobs.db --team test --kind slice --file starter_kit/examples/market_maker.py
python3 worker.py run --db /var/lib/quant-comp/jobs.db --mode docker --once
python3 worker.py status --db /var/lib/quant-comp/jobs.db
```

For the event, run two workers under systemd. Create `/etc/systemd/system/quant-worker@.service`:

```
[Unit]
Description=Quant comp grading worker %i
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=/home/YOURUSER/quant_comp
EnvironmentFile=/etc/quant-comp.env
ExecStart=/usr/bin/python3 worker.py run --db /var/lib/quant-comp/jobs.db --mode docker
Restart=always
User=YOURUSER

[Install]
WantedBy=multi-user.target
```

```
sudo systemctl daemon-reload
sudo systemctl enable --now quant-worker@1 quant-worker@2
journalctl -u quant-worker@1 -f
```

Two workers are safe: jobs are claimed atomically, in order, and a team never has two jobs running at once.
The worker needs access to the Docker socket, so `YOURUSER` must be in the `docker` group (you did this when installing Docker).
Run the API (see BACKEND_SETUP.md) as the same user so both can write the same database file.

## 5. Connecting the website (not built yet)

The website only needs two things from `worker.py`: `enqueue(conn, team, kind, path)` to add a job, and
reading the `jobs` table (`status`, `result_json`) to show results. Teams should only ever see their own rows.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `permission denied ... /var/run/docker.sock` | Run as root, or `sudo usermod -aG docker $USER` and log in again |
| `Unable to find image 'quant-comp-runner'` | Build it, and set `COMP_IMAGE` to the exact tag you built |
| Job status `error`, message "Internal grading error" | The runner crashed. The worker logs the container's stderr (`journalctl -u quant-worker@1`) |
| Infinite-loop test hangs instead of aborting | The container is missing `--cap-add KILL`. It is in `worker.docker_cmd`; don't remove it |
| `import pandas` fails with a MemoryError | Raise `SANDBOX_AS_MB` (default 4096) and the container's `--memory` together |
| Every pandas run times out on a small VPS | Check `--timing`. Raise `--cpus`, or use a larger server |
| Network check says `DID network` | The container is not running with `--network none`. Don't run the test through a different command |
| Containers pile up (`docker ps -a`) | Only happens if the worker is killed mid-job. `docker rm -f $(docker ps -aq --filter name=comp-job-)` |

To see exactly what the worker runs, print the command (no Docker needed):

```
COMP_SECRET_SEED=x python3 -c "import worker; print(' '.join(worker.docker_cmd('n', '/tmp/s.py', 'slice')))"
```

## Developing on a laptop

Docker Desktop (Mac, Windows) can build the image and run `test_docker.py`, since it runs a Linux VM, but the
mounted temp folder must be a path Docker Desktop shares, and I have not tested it there. If you only want to
exercise the queue without Docker, use `--mode local` (it still isolates the participant process, but has no network block).

## What has and has not been verified

- Verified here, without Docker: the process isolation, uid drop, timeout kill, resource limits, attack suite
  (`test_sandbox.py`), including a run with Docker's reduced capability set (SETUID, SETGID, KILL) emulated via `setpriv`.
- **Not verified: the Dockerfile build and the `docker run` flags.** No Docker was available. Treat the first
  run of `test_docker.py` as the real test, and expect to fix a small issue or two.
