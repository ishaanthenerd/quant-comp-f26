"""API test suite: python3 test_api.py   (run as root on Linux, like test_sandbox.py: it grades real jobs)
Auth is a website-signed Ed25519 JWT; see test_jwt_api.py for the token checks and the event stream."""
import json, os, subprocess, sys, tempfile, time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

tmp = tempfile.mkdtemp(prefix="apitest-")
KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
os.environ.update(COMP_DB=f"{tmp}/jobs.db", COMP_UPLOADS=f"{tmp}/uploads", COMP_COOLDOWN_S="0", BACKEND_JWT_PUBLIC_KEY=PUB,
                  EVENT_START="2020-01-01T00:00:00Z", EVENT_LOCK="2099-01-01T00:00:00Z", COMP_SECRET_SEED="api-test-seed")
os.environ.pop("COMP_DAILY_LIMIT", None)
from fastapi.testclient import TestClient
import app as appmod, common, worker

ok = True
def check(cond, msg, detail=""):
    global ok
    print(("PASS  " if cond else "FAIL  ") + msg + (f"   [{detail}]" if (detail and not cond) else ""))
    ok &= bool(cond)

def T(team):
    now = int(time.time())
    tok = jwt.encode({"sub": f"user-{team}", "team": team, "iss": "qfc-web", "aud": "qfc-backtest", "iat": now, "exp": now + 120},
                     KEY, algorithm="EdDSA")
    return {"Authorization": f"Bearer {tok}"}

conn = common.init_db(os.environ["COMP_DB"])
C = TestClient(appmod.app)
def up(team, code, fname="trader.py"):
    return C.post("/submissions", files={"file": (fname, code if isinstance(code, bytes) else code.encode())}, headers=T(team))
def run_worker(): worker.loop(os.environ["COMP_DB"], "local", once=True)
def err(r): return r.json().get("error")

GOOD = open("starter_kit/examples/market_maker.py").read()
BAD_IMPORT = "import os\nclass Trader:\n    def run(self, s): return {}, 0, ''\n"

# ---- public routes -------------------------------------------------------------------
check(C.get("/health").status_code == 200, "/health is public")
r = C.get("/event")
check(r.status_code == 200 and r.json()["state"] == "open" and r.json()["limits"]["max_bytes"] == 102400, "/event reports state and limits")
for path in ("/me", "/teams/mine", "/leaderboard", "/admin/queue"):
    check(C.get(path).status_code == 404, f"{path} no longer exists")

# ---- submission validation -----------------------------------------------------
check(C.post("/submissions", files={"file": ("t.py", b"x")}).status_code == 401, "upload requires a token")
check(err(up("Alpha", GOOD, "trader.txt")) == "invalid_file", "non-.py file rejected")
r = up("Alpha", "class Trader\n    pass\n"); check(err(r) == "syntax_error" and "line 1" in r.json()["message"], "syntax error rejected instantly with the line number", r.text)
check(up("Alpha", b"x" * 102401).status_code == 413, "file over 100 KB rejected (413)")
check(err(up("Alpha", b"\xff\xfe\x00bad")) == "invalid_file", "non-UTF-8 file rejected")
check(not os.path.exists(f"{tmp}/uploads/Alpha") or len(os.listdir(f"{tmp}/uploads/Alpha")) == 0, "rejected uploads leave no files behind")

# ---- rules: active job, queue, isolation ------------------------------------------
r = up("Alpha", GOOD); a1 = r.json().get("id")
check(r.status_code == 202 and r.json()["status"] == "queued", "valid upload is accepted and queued")
check(err(up("Alpha", GOOD)) == "active_job", "second upload while one is queued/running -> 409 active_job")
r = up("Beta", BAD_IMPORT); b1 = r.json().get("id")
ls = C.get("/submissions", headers=T("Alpha")).json()["submissions"]
check(len(ls) == 1 and ls[0]["status"] == "queued" and ls[0]["queue_position"] == 1, "list shows own submission with queue position")
check(C.get(f"/submissions/{a1}", headers=T("Beta")).status_code == 404, "a team cannot read another team's submission (404)")
check(all(s["id"] != a1 for s in C.get("/submissions", headers=T("Beta")).json()["submissions"]), "a team's list never includes other teams' submissions")

# ---- grading end to end -------------------------------------------------------------
run_worker()
r = C.get(f"/submissions/{a1}", headers=T("Alpha")).json()
res = r.get("result", {})
check(r["status"] == "done" and r["pnl"] > 0 and "pnl_by_product" in res and "p99_tick_ms" in res and "internal" not in res,
      "grader result appears on the submission (PnL, per-product, tick timing)", json.dumps(r)[:200])
r = C.get(f"/submissions/{b1}", headers=T("Beta")).json()
check(r["status"] == "error" and r["aborted"] is True and r["pnl"] == 0.0 and "not allowed" in r["message"], "disallowed import: error, aborted, PnL 0, clear message")

# ---- rate limits -------------------------------------------------------------------
os.environ["COMP_COOLDOWN_S"] = "300"
r = up("Alpha", GOOD); check(r.status_code == 429 and err(r) == "cooldown" and r.json()["retry_after_seconds"] > 0, "cooldown enforced with retry_after_seconds", r.text)
os.environ["COMP_COOLDOWN_S"] = "0"; os.environ["COMP_DAILY_LIMIT"] = "1"
check(err(up("Alpha", GOOD)) == "daily_limit", "daily limit enforced"); os.environ.pop("COMP_DAILY_LIMIT")
os.environ["EVENT_START"] = "2099-01-01T00:00:00Z"
check(err(up("Alpha", GOOD)) == "event_not_open", "uploads refused before the event starts")
os.environ["EVENT_START"] = "2020-01-01T00:00:00Z"; os.environ["EVENT_LOCK"] = "2021-01-01T00:00:00Z"
check(err(up("Alpha", GOOD)) == "event_locked", "uploads refused after the lock"); os.environ["EVENT_LOCK"] = "2099-01-01T00:00:00Z"

# ---- organiser CLI (on the VM, no HTTP admin) -------------------------------------------------
cli = lambda *a: subprocess.run([sys.executable, "manage.py", *a], capture_output=True, text=True, env=os.environ)
q = cli("queue").stdout
check("'done': 1" in q and "'error': 1" in q, "manage.py queue shows counts and recent jobs", q)
check(cli("rerun", str(a1)).stdout.strip() == "ok" and C.get(f"/submissions/{a1}", headers=T("Alpha")).json()["status"] == "queued", "manage.py rerun re-queues a finished job")
check(cli("rerun", str(a1)).returncode != 0, "re-running a job that is still queued is refused")
run_worker()
check(C.get(f"/submissions/{a1}", headers=T("Alpha")).json()["status"] == "done", "re-queued job completes")

# ---- final run (hidden day) -----------------------------------------------------------
out = common.enqueue_final_runs(conn)
check(sorted(out["queued"]) == ["Alpha", "Beta"], "final run queues each team's last submission", str(out))
check(common.enqueue_final_runs(conn)["queued"] == [], "final run is idempotent (no duplicates)")
run_worker()
full = {r["team"]: r["status"] for r in conn.execute("SELECT team, status FROM jobs WHERE kind='full'")}
check(full == {"Alpha": "done", "Beta": "error"}, "full runs grade like submissions", str(full))
check(C.get("/submissions", headers=T("Alpha")).json()["submissions"][0]["id"] == a1, "final-run jobs never appear in a team's own submission list")

print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED"); sys.exit(0 if ok else 1)
