# Running the API on the VM (organiser steps)

Do these on the VM, in the `quant_comp` folder, after the Docker steps in `DOCKER_SETUP.md` (image built, smoke test passing).
Replace `YOURUSER` with your login name on the VM (`whoami`).

## 1. Python environment and data folder
```
sudo apt-get install -y python3-venv
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo mkdir -p /var/lib/quant-comp && sudo chown $USER /var/lib/quant-comp
```

## 2. Settings
Add these to `/etc/quant-comp.env` (the file from `DOCKER_SETUP.md` already holds `COMP_SECRET_SEED` and `COMP_IMAGE`):
```
echo "COMP_DB=/var/lib/quant-comp/jobs.db"       | sudo tee -a /etc/quant-comp.env >/dev/null
echo "COMP_UPLOADS=/var/lib/quant-comp/uploads"  | sudo tee -a /etc/quant-comp.env >/dev/null
```
and the website's **public** key, which verifies the JWT on every `/submissions` request (newlines written as `\n`, in single quotes):
```
BACKEND_JWT_PUBLIC_KEY='-----BEGIN PUBLIC KEY-----\nMCow...\n-----END PUBLIC KEY-----\n'
```
The private key stays on the website (Vercel) only. That JWT is the only auth in this service: sign-in, teams, the tenant check
and the standings are all the website's.
Do **not** add `EVENT_START` or `EVENT_LOCK`: the defaults are the real event (start Nov 11 02:00 UTC, lock Nov 17 02:00 UTC).
Load the file into your shell whenever you run `manage.py` or `worker.py` by hand:
```
set -a; . /etc/quant-comp.env; set +a
```

## 4. Start the services
Create `/etc/systemd/system/quant-api.service`:
```
[Unit]
Description=Quant comp API
After=network.target

[Service]
User=YOURUSER
WorkingDirectory=/home/YOURUSER/quant_comp
EnvironmentFile=/etc/quant-comp.env
ExecStart=/home/YOURUSER/quant_comp/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```
Make sure the worker unit from `DOCKER_SETUP.md` also says `User=YOURUSER` (not root), then:
```
sudo systemctl daemon-reload
sudo systemctl enable --now quant-api quant-worker@1 quant-worker@2
sudo systemctl status quant-api quant-worker@1 --no-pager
```
uvicorn listens on localhost only. **Caddy** serves it over HTTPS (`sudo apt-get install -y caddy`), with `/etc/caddy/Caddyfile`:
```
136-114-102-181.sslip.io {
	reverse_proxy 127.0.0.1:8000
}
```
(`sslip.io` resolves that name to the VM's static IP; swap in a real domain any time.) Caddy gets and renews the certificate
itself. The firewall rule `allow-https` (tcp:80, tcp:443) must exist; port 8000 stays closed to the internet.

## 5. Check it from outside
```
curl https://136-114-102-181.sslip.io/event           # JSON with "state": "before"
curl https://136-114-102-181.sslip.io/submissions     # 401 not_logged_in (needs the website's JWT)
```
The website's `BACKTEST_API_URL` is `https://136-114-102-181.sslip.io`.

## 6. Dry run (Nov 9)
Submissions are refused until the start time, so for the dry run add `EVENT_START=2020-01-01T00:00:00Z` to the env file,
`sudo systemctl restart quant-api`, and try everything through the real website: upload, good and bad traders, the limits.
**Then remove that line, clean up, and restart**, or the event will open early with leftover data:
```
sudo systemctl stop quant-api quant-worker@1 quant-worker@2
# delete the line EVENT_START=... from /etc/quant-comp.env
python3 -c "import sqlite3; c=sqlite3.connect('/var/lib/quant-comp/jobs.db'); c.execute('DELETE FROM jobs'); c.commit()"
rm -rf /var/lib/quant-comp/uploads
sudo systemctl start quant-api quant-worker@1 quant-worker@2
curl https://136-114-102-181.sslip.io/event      # "state": "before" again
```

## 7. Backups
```
python3 -c "import sqlite3,time; s=sqlite3.connect('/var/lib/quant-comp/jobs.db'); d=sqlite3.connect('/var/lib/quant-comp/backup-%d.db'%time.time()); s.backup(d)"
```
Run that (cron or by hand) every few hours, and pull a copy to your computer with
`gcloud compute scp quant-comp:/var/lib/quant-comp/backup-*.db . --zone=us-central1-a --tunnel-through-iap`.
Uploaded code lives in `/var/lib/quant-comp/uploads`; copy that folder too.

## 8. Lock night (Nov 16, 8pm Central)
Submissions stop automatically at the lock. Then:
```
set -a; . /etc/quant-comp.env; set +a
python3 manage.py final-run        # queues a full-data run of each team's LAST submission
python3 manage.py queue            # watch until no full run is queued or running
python3 manage.py rerun ID         # re-queue a job that hit an internal grading error
```
Final results are the `kind='full'` rows in the jobs table. Ranking and publishing them is the website's job.
