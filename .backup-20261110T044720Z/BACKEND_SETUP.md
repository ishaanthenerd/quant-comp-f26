# Running the API on the VM (organiser steps)

Do these on the VM, in the `quant_comp` folder, after the Docker steps in `DOCKER_SETUP.md` (image built, smoke test passing).
Replace `YOURUSER` with your login name on the VM (`whoami`).

## 1. Python environment and data folder
```
sudo apt-get install -y python3-venv
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo mkdir -p /var/lib/quant-comp && sudo chown $USER /var/lib/quant-comp
```

## 2. Secrets and settings
Add these to `/etc/quant-comp.env` (the file from `DOCKER_SETUP.md` already holds `COMP_SECRET_SEED` and `COMP_IMAGE`):
```
echo "PROXY_SECRET=$(openssl rand -hex 24)"  | sudo tee -a /etc/quant-comp.env >/dev/null
echo "ADMIN_TOKEN=$(openssl rand -hex 24)"   | sudo tee -a /etc/quant-comp.env >/dev/null
echo "COMP_DB=/var/lib/quant-comp/jobs.db"       | sudo tee -a /etc/quant-comp.env >/dev/null
echo "COMP_UPLOADS=/var/lib/quant-comp/uploads"  | sudo tee -a /etc/quant-comp.env >/dev/null
sudo cat /etc/quant-comp.env          # read PROXY_SECRET from here to send to your friend
```
Do **not** add `EVENT_START` or `EVENT_LOCK`: the defaults are the real event (start Nov 11 02:00 UTC, lock Nov 17 02:00 UTC).
Load the file into your shell whenever you run `manage.py` or `worker.py` by hand:
```
set -a; . /etc/quant-comp.env; set +a
```

## 3. Team sign-up (nothing to register)
Anyone who signs in with Microsoft can create a team (and gets a join code to share) or join one with a code, until the lock. You keep no list.
Three settings make this safe; put them in `/etc/quant-comp.env` (all optional, defaults shown):
```
ALLOWED_TENANT_ID=<your university's Directory (tenant) ID>   # strongly recommended: only university accounts can use the API
COMP_MAX_TEAM_SIZE=5
COMP_MAX_TEAMS=60
```
Find the tenant id in the Microsoft Entra admin center: the app registration's Overview page shows "Directory (tenant) ID". Ask your friend to register the
app as **single-tenant** too. Without a tenant limit, anyone with any Microsoft account could sign up, create teams, and queue code on your VM.

Handy commands (load the env first: `set -a; . /etc/quant-comp.env; set +a`):
```
python3 manage.py list-teams                       # every team, its join code, members, whether they signed in
python3 manage.py move-member EMAIL --team NAME    # people cannot leave a team themselves
python3 manage.py new-code NAME                    # if a join code leaks
python3 manage.py remove-team NAME                 # spam or test teams
```
Optional: pre-register people with `manage.py import-members members.csv` (columns `email,team`): they are linked to that team the first time they sign in.
Set `SIGNUP_MODE=invite` to switch self sign-up off and allow only pre-registered accounts.

**Ranking note:** teams people created themselves and never submitted with are left off the final ranking. Teams you pre-registered that never submitted
are ranked as "no submission" (PnL 0, below the other zero-PnL teams). Delete your own test teams (`remove-team`) before the event.

The API runs in `AUTH_MODE=proxy` (the default), which **requires** `PROXY_SECRET`: whoever knows it can claim to be anyone, so keep it private and only in the env file and Vercel.

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
ExecStart=/home/YOURUSER/quant_comp/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
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
The firewall rule `allow-api` (tcp:8000) from the Google Cloud steps must exist.

## 5. Check it from outside
From your own computer (use the VM's fixed IP, and the `PROXY_SECRET` value):
```
curl -H "x-proxy-secret: THE_SECRET" http://VM_IP:8000/event       # JSON with "state": "before"
curl http://VM_IP:8000/event                                        # must say forbidden
```
Then send your friend `BACKEND_URL=http://VM_IP:8000` and the `PROXY_SECRET`, plus `API.md`.

## 6. Dry run (Nov 9)
Submissions are refused until the start time, so for the dry run add `EVENT_START=2020-01-01T00:00:00Z` to the env file,
`sudo systemctl restart quant-api`, and try everything through the real website: login, upload, good and bad traders, the limits.
**Then remove that line, clean up, and restart**, or the event will open early with leftover data:
```
sudo systemctl stop quant-api quant-worker@1 quant-worker@2
# delete the line EVENT_START=... from /etc/quant-comp.env
python3 -c "import sqlite3; c=sqlite3.connect('/var/lib/quant-comp/jobs.db'); [c.execute(q) for q in ('DELETE FROM jobs','DELETE FROM sessions','DELETE FROM login_failures')]; c.commit()"
rm -rf /var/lib/quant-comp/uploads
sudo systemctl start quant-api quant-worker@1 quant-worker@2
curl -H "x-proxy-secret: THE_SECRET" http://VM_IP:8000/event      # "state": "before" again
```

## 7. Backups
```
python3 -c "import sqlite3,time; s=sqlite3.connect('/var/lib/quant-comp/jobs.db'); d=sqlite3.connect('/var/lib/quant-comp/backup-%d.db'%time.time()); s.backup(d)"
```
Run that (cron or by hand) every few hours, and pull a copy to your computer with
`gcloud compute scp quant-comp:/var/lib/quant-comp/backup-*.db . --zone=us-central1-a --tunnel-through-iap`.
Uploaded code lives in `/var/lib/quant-comp/uploads`; copy that folder too.

## 8. Lock night (Nov 16, 8pm Central) and results
Submissions stop automatically at the lock. Then:
```
set -a; . /etc/quant-comp.env; set +a
python3 manage.py final-run        # queues a full-data run of each team's LAST submission
python3 manage.py ranking          # re-run until no team shows "full run not finished"
```
It lists teams with **no submission**: they score PnL 0 and rank below every other zero-PnL team (and above negative ones). A broken last submission counts as the team's final one (PnL 0, ranked among the aborted runs, above no-submission teams). On Nov 17, after fixing anything wrong
(`/admin/rerun/{id}` re-queues a job, or re-run `final-run --force`), publish with:
```
python3 manage.py release          # refuses while any result is incomplete or has a grading error
python3 manage.py unrelease        # hide it again if needed
```
Admin calls also work over HTTP with the `X-Admin-Token` header: `GET /admin/queue`, `GET /admin/teams`, `POST /admin/rerun/{id}`, `POST /admin/release`.
