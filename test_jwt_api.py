"""JWT auth + submission events: python3 test_jwt_api.py   (no Docker needed; jobs are finished by hand)"""
import json, os, sqlite3, tempfile, threading, time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

tmp = tempfile.mkdtemp(prefix="jwttest-")
key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
os.environ.update(COMP_DB=f"{tmp}/jobs.db", COMP_UPLOADS=f"{tmp}/uploads", COMP_COOLDOWN_S="0",
                  EVENT_START="2020-01-01T00:00:00Z", EVENT_LOCK="2099-01-01T00:00:00Z",
                  BACKEND_JWT_PUBLIC_KEY=pub.replace("\n", "\\n"))   # stored with escaped newlines, as in the env file
from fastapi.testclient import TestClient
import app as appmod

ok = True
def check(cond, msg, detail=""):
    global ok
    print(("PASS  " if cond else "FAIL  ") + msg + (f"   [{detail}]" if (detail and not cond) else ""))
    ok &= bool(cond)

def token(team="team-a", k=key, ttl=120, **over):
    now = int(time.time())
    claims = {"sub": "user-1", "team": team, "iss": "qfc-web", "aud": "qfc-backtest", "iat": now, "exp": now + ttl, **over}
    claims = {k2: v for k2, v in claims.items() if v is not None}
    return jwt.encode(claims, k, algorithm="EdDSA")

def H(**kw): return {"Authorization": "Bearer " + token(**kw)}
def err(r): return r.json().get("error")
TRADER = b"class Trader:\n    def run(self, state):\n        return {}, 0, state.trader_data\n"
def up(headers, code=TRADER): return C.post("/submissions", files={"file": ("trader.py", code)}, headers=headers)
def finish(jid, pnl):
    with sqlite3.connect(os.environ["COMP_DB"]) as c:
        c.execute("UPDATE jobs SET status='done', result_json=? WHERE id=?",
                  (json.dumps({"pnl": pnl, "aborted": False, "message": "", "pnl_by_product": {"ALPHA": pnl}, "max_drawdown": 1.5,
                               "pnl_curve": [0, pnl]}), jid))

C = TestClient(appmod.app)
check(C.get("/health").json() == {"ok": True}, "/health is public")
check(C.get("/event").status_code == 200 and "results_released" not in C.get("/event").json(), "/event is public, no leaderboard fields")
check(C.get("/leaderboard").status_code == 404, "no /leaderboard: standings live on the website")
for path in ("/me", "/teams/mine", "/auth/login"):
    check(C.get(path).status_code in (404, 405), f"{path} is gone")

check(err(C.get("/submissions")) == "not_logged_in" and C.get("/submissions").status_code == 401, "no token -> 401 not_logged_in")
check(C.get("/submissions", headers=H(k=other)).status_code == 401, "token signed with another key -> 401")
check(C.get("/submissions", headers=H(ttl=-60)).status_code == 401, "expired token -> 401")
check(C.get("/submissions", headers=H(aud="someone-else")).status_code == 401, "wrong audience -> 401")
check(C.get("/submissions", headers=H(iss="evil")).status_code == 401, "wrong issuer -> 401")
check(C.get("/submissions", headers=H(team=None)).status_code == 401, "token without a team -> 401")
check(C.get("/submissions", headers={"Authorization": "Basic abc"}).status_code == 401, "non-Bearer auth -> 401")
check(C.get("/submissions", headers=H()).json() == {"submissions": []}, "valid token -> the team's (empty) list")

r = up(H())
check(r.status_code == 202 and r.json()["status"] == "queued", "submit with a valid token -> 202 queued", r.text)
jid = r.json()["id"]
check(err(up(H())) == "active_job", "limits still apply per team (active_job)")
check(up(H(team="team-b")).status_code == 202, "another team submits independently")
check(C.get(f"/submissions/{jid}", headers=H(team="team-b")).status_code == 404, "teams can't read each other's submissions")
check(err(C.post("/submissions", files={"file": ("t.py", b"def (:")}, headers=H(team="team-c"))) == "syntax_error", "syntax errors still caught")

# SSE: status while queued, then done (with result) once the job finishes, then the stream ends.
threading.Timer(1.2, finish, (jid, 42.5)).start()
events, t0 = [], time.time()
with C.stream("GET", f"/submissions/{jid}/events", headers=H()) as s:
    check(s.headers["content-type"].startswith("text/event-stream"), "events endpoint streams text/event-stream")
    ev = None
    for line in s.iter_lines():
        if line.startswith("event:"): ev = line[6:].strip()
        elif line.startswith("data:"): events.append((ev, json.loads(line[5:])))
check(events and events[0][0] == "status" and events[0][1]["status"] == "queued", "first event: status queued", events[:1])
check(events and events[-1][0] == "done" and events[-1][1]["result"]["pnl"] == 42.5, "last event: done with the result", events[-1:])
check(time.time() - t0 < 5, "stream closes soon after the job finishes")
check(C.get(f"/submissions/{jid}/events", headers=H(team="team-b")).status_code == 404, "another team's events -> 404")
for path in ("/admin/queue", "/admin/teams"):
    check(C.get(path, headers={"x-admin-token": "anything"}).status_code == 404, f"{path} is gone (organiser ops are manage.py on the VM)")

# ---- limits: 5-minute cooldown, 15 a day resetting at midnight Central ----------------------------
import common
def add_job(team, created, status="done"):
    with sqlite3.connect(os.environ["COMP_DB"]) as c:
        c.execute("INSERT INTO jobs(team, kind, submission_path, created, status) VALUES (?,?,?,?,?)",
                  (team, "slice", "/dev/null", created, status))
os.environ["COMP_COOLDOWN_S"] = "300"
add_job("team-cool", time.time() - 299)
r = up(H(team="team-cool"))
check(err(r) == "cooldown" and 0 < r.json()["retry_after_seconds"] <= 2, "submitting 299 s after the last one -> cooldown", r.text)
add_job("team-warm", time.time() - 301)
check(up(H(team="team-warm")).status_code == 202, "301 s after the last one -> accepted")
os.environ["COMP_COOLDOWN_S"] = "0"

midnight = common.day_start()
for i in range(15):
    add_job("team-day", midnight - 3600 - i)            # yesterday (Central): does not count today
for i in range(14):
    add_job("team-day", midnight + 1 + i)               # today: 14 used
check(up(H(team="team-day")).status_code == 202, "15th submission today is accepted (yesterday's don't count)")
with sqlite3.connect(os.environ["COMP_DB"]) as c:
    c.execute("UPDATE jobs SET status='done' WHERE team='team-day'")
r = up(H(team="team-day"))
until_midnight = common.next_day_start() - time.time()
check(err(r) == "daily_limit" and abs(r.json()["retry_after_seconds"] - until_midnight) < 5 and "midnight Central" in r.json()["message"],
      "16th today -> daily_limit, retry_after_seconds counts down to midnight Central", r.text)
check(common.next_day_start() - common.day_start() in (82800, 86400, 90000), "a Central day is 23, 24 or 25 hours (DST-aware)")
from datetime import datetime, timezone
t = datetime(2026, 11, 10, 5, 59, 59, tzinfo=timezone.utc).timestamp()
check(common.iso(common.day_start(t)) == "2026-11-09T06:00:00Z" and common.iso(common.next_day_start(t)) == "2026-11-10T06:00:00Z",
      "11:59:59 pm CST still belongs to that day; the next starts at 06:00 UTC")

print("\nALL PASSED" if ok else "\nSOME FAILED")
raise SystemExit(0 if ok else 1)
