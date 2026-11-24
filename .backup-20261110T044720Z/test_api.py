"""API test suite: python3 test_api.py   (run as root on Linux, like test_sandbox.py: it grades real jobs)"""
import os, sys, tempfile, time, json, sqlite3

tmp = tempfile.mkdtemp(prefix="apitest-")
os.environ.update(COMP_DB=f"{tmp}/jobs.db", COMP_UPLOADS=f"{tmp}/uploads", COOKIE_SECURE="0",
                  PROXY_SECRET="proxy-secret", ADMIN_TOKEN="admin-token", COMP_COOLDOWN_S="0",
                  EVENT_START="2020-01-01T00:00:00Z", EVENT_LOCK="2099-01-01T00:00:00Z", COMP_SECRET_SEED="api-test-seed", AUTH_MODE="password")
os.environ.pop("COMP_DAILY_LIMIT", None)
from fastapi.testclient import TestClient
import app as appmod, common, worker

ok = True
def check(cond, msg, detail=""):
    global ok
    print(("PASS  " if cond else "FAIL  ") + msg + (f"   [{detail}]" if (detail and not cond) else ""))
    ok &= bool(cond)

H = {"x-proxy-secret": "proxy-secret"}
ADM = {**H, "x-admin-token": "admin-token"}
conn = common.init_db(os.environ["COMP_DB"])
PW = {}
for t in ("Alpha", "Beta", "Gamma", "Delta"):
    PW[t] = common.new_password()
    conn.execute("INSERT INTO teams(name,password_hash,created) VALUES (?,?,?)", (t, common.hash_password(PW[t]), time.time()))

def client(): return TestClient(appmod.app)
def login(c, team, pw=None): return c.post("/auth/login", json={"team": team, "password": pw if pw is not None else PW[team]}, headers=H)
def up(c, name, code, fname="trader.py"): return c.post("/submissions", files={"file": (fname, code if isinstance(code, bytes) else code.encode())}, headers=H)
def run_worker(): worker.loop(os.environ["COMP_DB"], "local", once=True)
def err(r): return r.json().get("error")

GOOD = open("starter_kit/examples/market_maker.py").read()
BAD_IMPORT = "import os\nclass Trader:\n    def run(self, s): return {}, 0, ''\n"

# ---- guards -------------------------------------------------------------------
A, B, G, D = client(), client(), client(), client()
check(A.get("/health").status_code == 200, "/health works without the proxy secret")
check(A.get("/event").status_code == 403, "requests without the proxy secret are refused")
r = A.get("/event", headers=H)
check(r.status_code == 200 and r.json()["state"] == "open" and r.json()["limits"]["max_bytes"] == 102400, "/event reports state and limits")
check(A.get("/me", headers=H).status_code == 401 and err(A.get("/me", headers=H)) == "not_logged_in", "/me requires login")

# ---- auth ---------------------------------------------------------------------
r1 = login(A, "Alpha", "wrong"); r2 = login(A, "Nobody", "wrong")
check(r1.status_code == 401 and r2.status_code == 401 and r1.json() == r2.json(), "wrong password and unknown team give the same 401")
check(A.post("/auth/login", content=b"not json", headers={**H, "content-type": "application/json"}).status_code == 400, "malformed login body gives 400")
r = login(A, "alpha", PW["Alpha"])      # case-insensitive team name
check(r.status_code == 200 and r.json()["team"] == "Alpha", "login works, case-insensitive, returns canonical name")
check(A.get("/me", headers=H).json() == {"team": "Alpha"}, "session cookie authenticates /me")
login(B, "Beta"); login(D, "Delta")
for _ in range(10): login(G, "Gamma", "bad")
check(login(G, "Gamma").status_code == 429, "10 failed logins lock the team out (even with the right password)")
os.environ["COOKIE_SECURE"] = "1"
sc = TestClient(appmod.app, base_url="https://testserver"); r = login(sc, "Delta")
cookie = r.headers.get("set-cookie", "")
check("HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower(), "session cookie is HttpOnly, Secure, SameSite=Lax by default", cookie)
os.environ["COOKIE_SECURE"] = "0"
conn.execute("INSERT INTO teams(name,password_hash,created) VALUES ('Epsilon',NULL,?)", (time.time(),))
check(login(client(), "Epsilon", "dummy-password").status_code == 401 and login(client(), "Epsilon", "").status_code == 401,
      "a team with no password can never log in with a password (including the internal dummy one)")
conn.execute("DELETE FROM teams WHERE name='Epsilon'")
check(common.verify_password(PW["Alpha"], conn.execute("select password_hash from teams where name='Alpha'").fetchone()[0]) and
      PW["Alpha"] not in conn.execute("select password_hash from teams where name='Alpha'").fetchone()[0], "passwords are stored hashed, not in plain text")
lo = client(); login(lo, "Delta"); lo.post("/auth/logout", headers=H)
check(lo.get("/me", headers=H).status_code == 401, "logout ends the session")

# ---- submission validation -----------------------------------------------------
check(client().post("/submissions", files={"file": ("t.py", b"x")}, headers=H).status_code == 401, "upload requires login")
check(err(up(A, "a", GOOD, "trader.txt")) == "invalid_file", "non-.py file rejected")
r = up(A, "a", "class Trader\n    pass\n"); check(err(r) == "syntax_error" and "line 1" in r.json()["message"], "syntax error rejected instantly with the line number", r.text)
check(up(A, "a", b"x" * 102401).status_code == 413, "file over 100 KB rejected (413)")
check(err(up(A, "a", b"\xff\xfe\x00bad")) == "invalid_file", "non-UTF-8 file rejected")
check(not os.path.exists(f"{tmp}/uploads/Alpha") or len(os.listdir(f"{tmp}/uploads/Alpha")) == 0, "rejected uploads leave no files behind")

# ---- rules: active job, queue, isolation ------------------------------------------
r = up(A, "a", GOOD); a1 = r.json().get("id")
check(r.status_code == 202 and r.json()["status"] == "queued", "valid upload is accepted and queued")
check(err(up(A, "a", GOOD)) == "active_job", "second upload while one is queued/running -> 409 active_job")
r = up(B, "b", BAD_IMPORT); b1 = r.json().get("id")
ls = A.get("/submissions", headers=H).json()["submissions"]
check(len(ls) == 1 and ls[0]["status"] == "queued" and ls[0]["queue_position"] == 1, "list shows own submission with queue position")
check(B.get(f"/submissions/{a1}", headers=H).status_code == 404, "a team cannot read another team's submission (404)")
check(all(s["id"] != a1 for s in B.get("/submissions", headers=H).json()["submissions"]), "a team's list never includes other teams' submissions")

# ---- grading end to end -------------------------------------------------------------
run_worker()
r = A.get(f"/submissions/{a1}", headers=H).json()
res = r.get("result", {})
check(r["status"] == "done" and r["pnl"] > 0 and "pnl_by_product" in res and "p99_tick_ms" in res and "internal" not in res,
      "grader result appears on the submission (PnL, per-product, tick timing)", json.dumps(r)[:200])
r = B.get(f"/submissions/{b1}", headers=H).json()
check(r["status"] == "error" and r["aborted"] is True and r["pnl"] == 0.0 and "not allowed" in r["message"], "disallowed import: error, aborted, PnL 0, clear message")

# ---- rate limits -------------------------------------------------------------------
os.environ["COMP_COOLDOWN_S"] = "300"
r = up(A, "a", GOOD); check(r.status_code == 429 and err(r) == "cooldown" and r.json()["retry_after_seconds"] > 0, "cooldown enforced with retry_after_seconds", r.text)
os.environ["COMP_COOLDOWN_S"] = "0"; os.environ["COMP_DAILY_LIMIT"] = "1"
check(err(up(A, "a", GOOD)) == "daily_limit", "daily limit enforced"); os.environ.pop("COMP_DAILY_LIMIT")
os.environ["EVENT_START"] = "2099-01-01T00:00:00Z"
check(err(up(A, "a", GOOD)) == "event_not_open", "uploads refused before the event starts")
os.environ["EVENT_START"] = "2020-01-01T00:00:00Z"; os.environ["EVENT_LOCK"] = "2021-01-01T00:00:00Z"
check(err(up(A, "a", GOOD)) == "event_locked", "uploads refused after the lock"); os.environ["EVENT_LOCK"] = "2099-01-01T00:00:00Z"

# ---- admin -----------------------------------------------------------------------------
check(A.get("/admin/queue", headers=H).status_code == 403, "admin endpoints refuse requests without the admin token")
q = A.get("/admin/queue", headers=ADM).json(); check(q["counts"].get("done") == 1 and q["counts"].get("error") == 1, "admin queue overview works", str(q))
check(len(A.get("/admin/teams", headers=ADM).json()["teams"]) == 4, "admin team list works")
check(A.post(f"/admin/rerun/{a1}", headers=ADM).status_code == 200 and A.get(f"/submissions/{a1}", headers=H).json()["status"] == "queued", "admin can re-queue a finished job")
check(A.post(f"/admin/rerun/{a1}", headers=ADM).status_code == 409, "re-running a job that is still queued is refused")
run_worker()
check(A.get(f"/submissions/{a1}", headers=H).json()["status"] == "done", "re-queued job completes")

# ---- final run, leaderboard, release ----------------------------------------------------
check(err(A.get("/leaderboard", headers=H)) == "results_not_released", "leaderboard hidden until released")
check(A.post("/admin/release", json={"released": True}, headers=ADM).status_code == 409, "release refused while no final results exist")
out = common.enqueue_final_runs(conn)
check(sorted(out["queued"]) == ["Alpha", "Beta"] and sorted(out["no_submission"]) == ["Delta", "Gamma"], "final run queues each team's last submission; teams with none are reported", str(out))
check(common.enqueue_final_runs(conn)["queued"] == [], "final run is idempotent (no duplicates)")
check(err(A.post("/admin/release", json={"released": True}, headers=ADM)) == "not_ready", "release refused while full runs are unfinished")
run_worker()
ranked, problems = common.build_leaderboard(conn)
check([(rk, t) for rk, t, _ in ranked] == [(1, "Alpha"), (2, "Beta"), (3, "Delta"), (3, "Gamma")] and not problems,
      "ranking: profitable run, then aborted (PnL 0), then teams with no submission (PnL 0)", str(ranked))
conn.execute("UPDATE jobs SET result_json=? WHERE team='Beta' AND kind='full'", (json.dumps({"status": "error", "internal": True}),))
check(err(A.post("/admin/release", json={"released": True}, headers=ADM)) == "not_ready", "an internal grading error blocks release")
conn.execute("UPDATE jobs SET result_json=? WHERE team='Beta' AND kind='full'", (json.dumps({"status": "error", "pnl": 0.0, "aborted": True, "message": "x"}),))
check(A.post("/admin/release", json={"released": True}, headers=ADM).status_code == 200, "release succeeds when every full run is clean")
lb = A.get("/leaderboard", headers=H).json()["leaderboard"]
check([e["team"] for e in lb] == ["Alpha", "Beta", "Delta", "Gamma"] and lb[1]["aborted"] is True and lb[2]["no_submission"] is True
      and lb[2]["pnl"] == 0 and "logs" not in lb[0], "public leaderboard shows rank/PnL only (no logs); no-submission teams flagged")
check(A.get("/submissions", headers=H).json()["submissions"][0]["id"] == a1, "final-run jobs never appear in a team's own submission list")

# ---- proxy (Microsoft) auth mode -----------------------------------------------------------
os.environ["AUTH_MODE"] = "proxy"; os.environ["SIGNUP_MODE"] = "invite"
for em, team in (("alice@uni.edu", "Alpha"), ("bob@uni.edu", "Alpha"), ("carol@uni.edu", "Beta")):
    conn.execute("INSERT INTO members(email, team) VALUES (?,?)", (em, team))
def P(uid=None, email=None, extra=None):
    h = dict(H)
    if uid: h["x-user-id"] = uid
    if email: h["x-user-email"] = email
    return {**h, **(extra or {})}
C = client()
check(C.get("/me", headers=H).status_code == 401, "proxy mode: no identity headers -> 401 not_logged_in")
check(err(C.get("/me", headers=P("u-nobody"))) == "not_registered", "proxy mode: unknown account -> 403 not_registered")
check(err(C.get("/me", headers=P("u-x", "stranger@uni.edu"))) == "not_registered", "proxy mode: unregistered email -> 403 not_registered")
r = C.get("/me", headers=P("u-alice", "ALICE@uni.edu"))
check(r.status_code == 200 and r.json() == {"team": "Alpha", "email": "alice@uni.edu", "signup": "invite"}, "first sign-in links the account to its team (email is case-insensitive)", r.text)
check(conn.execute("SELECT user_id FROM members WHERE email='alice@uni.edu'").fetchone()[0] == "u-alice", "the Microsoft user id is stored on the member")
check(C.get("/me", headers=P("u-alice")).json()["team"] == "Alpha", "afterwards the stable id alone identifies the team (no email needed)")
r = C.get("/me", headers=P("u-evil", "alice@uni.edu"))
check(r.status_code == 403 and err(r) == "account_mismatch", "a different account claiming alice's email is refused (account_mismatch)")
check(err(C.get("/me", headers=P("u-alice", "carol@uni.edu"))) is None and C.get("/me", headers=P("u-alice", "carol@uni.edu")).json()["team"] == "Alpha",
      "a linked account keeps its team even if it presents someone else's email")
check(C.get("/me", headers=P("u-bob", "bob@uni.edu")).json()["team"] == "Alpha", "several accounts can share one team")
check(C.get("/me", headers=P("u-carol", "carol@uni.edu")).json()["team"] == "Beta", "another team's member maps to its own team")
check(any(s["id"] == a1 for s in C.get("/submissions", headers=P("u-bob")).json()["submissions"]), "teammates see the same submissions")
check(C.get(f"/submissions/{a1}", headers=P("u-carol")).status_code == 404, "proxy mode keeps teams isolated (404 on another team's submission)")
r = C.post("/submissions", files={"file": ("t.py", GOOD.encode())}, headers=P("u-bob")); check(r.status_code == 202, "a team member can submit in proxy mode", r.text)
check(err(C.post("/submissions", files={"file": ("t.py", GOOD.encode())}, headers=P("u-alice"))) == "active_job", "team limits are shared between teammates (one active job per team)")
check(C.post("/auth/login", json={"team": "Alpha", "password": "x"}, headers=P("u-alice")).status_code == 404, "password login is switched off in proxy mode")
check(C.get("/me", headers={"x-user-id": "u-alice"}).status_code == 403, "identity headers without the proxy secret are refused, so they cannot be forged")
sec = os.environ.pop("PROXY_SECRET")
check(C.get("/me", headers={"x-user-id": "u-alice"}).status_code == 500, "proxy mode refuses to run without PROXY_SECRET (misconfiguration, not a security hole)")
os.environ["PROXY_SECRET"] = sec

# ---- organiser CLI ------------------------------------------------------------------------
import subprocess
open(f"{tmp}/members.csv", "w").write("email,team\ndave@uni.edu,Zeta\nerin@uni.edu,Zeta\nnot-an-email,Zeta\ncarol@uni.edu,Zeta\n")
cli = lambda *a: subprocess.run([sys.executable, "manage.py", *a], capture_output=True, text=True, env=os.environ)
r = cli("import-members", f"{tmp}/members.csv")
check("Added 2 members" in r.stdout and "skip bad row" in r.stdout and "already on team Beta" in r.stdout, "import-members adds members, skips bad rows and cross-team duplicates", r.stdout + r.stderr)
check(conn.execute("SELECT password_hash FROM teams WHERE name='Zeta'").fetchone()[0] is None, "teams created by import-members have no password")
check(cli("unbind-member", "alice@uni.edu").stdout.strip() == "ok" and conn.execute("SELECT user_id FROM members WHERE email='alice@uni.edu'").fetchone()[0] is None, "unbind-member clears the linked account")
check("(not signed in yet)" in cli("list-teams").stdout, "list-teams shows who has not signed in yet")
check(cli("remove-team", "Zeta").stdout.strip() == "ok" and conn.execute("SELECT COUNT(*) FROM members WHERE team='Zeta'").fetchone()[0] == 0, "remove-team removes the team and its members")

# ---- open sign-up: anyone who signs in can create or join a team ---------------------------------
os.environ["SIGNUP_MODE"] = "open"
S1, S2, S3, S4 = client(), client(), client(), client()
r = S1.get("/me", headers=P("s1", "s1@uni.edu"))
check(r.status_code == 200 and r.json()["team"] is None and r.json()["signup"] == "open", "open sign-up: a new account is signed in with no team yet", r.text)
check(err(S1.get("/submissions", headers=P("s1"))) == "no_team", "open sign-up: submitting needs a team first (403 no_team)")
r = S1.post("/teams", json={"name": "  Rocket   Club "}, headers=P("s1", "s1@uni.edu"))
check(r.status_code == 201 and r.json()["team"] == "Rocket Club" and len(r.json()["join_code"]) == 8, "creating a team returns a join code (name whitespace normalised)", r.text)
code = r.json()["join_code"]
check(S1.get("/me", headers=P("s1")).json()["team"] == "Rocket Club", "the creator is on the new team")
check(err(S1.post("/teams", json={"name": "Another"}, headers=P("s1"))) == "already_in_team", "an account can only be on one team")
check(err(S2.post("/teams", json={"name": "rocket club"}, headers=P("s2"))) == "name_taken", "team names are unique, case-insensitively")
check(err(S2.post("/teams", json={"name": "x"}, headers=P("s2"))) == "invalid_name" and err(S2.post("/teams", json={"name": "<script>"}, headers=P("s2"))) == "invalid_name", "invalid team names are rejected")
check(err(S2.post("/teams/join", json={"code": "ZZZZZZZZ"}, headers=P("s2"))) == "invalid_code", "a wrong join code is rejected")
r = S2.post("/teams/join", json={"code": code.lower()}, headers=P("s2", "s2@uni.edu", {"x-user-name": "Sam Lee"}))
check(r.status_code == 200 and r.json()["team"] == "Rocket Club", "joining with the code works (case-insensitive)", r.text)
mine = S2.get("/teams/mine", headers=P("s2")).json()
check(mine["join_code"] == code and [m["name"] for m in mine["members"]] == ["s1", "Sam Lee"] and mine["max_size"] == 5, "team page shows the code and members' display names", str(mine))
r = S1.post("/submissions", files={"file": ("t.py", GOOD.encode())}, headers=P("s1")); check(r.status_code == 202, "a self-created team can submit", r.text)
check(any(s["id"] == r.json()["id"] for s in S2.get("/submissions", headers=P("s2")).json()["submissions"]), "teammates who joined by code see the team's submissions")
check(err(S3.post("/submissions", files={"file": ("t.py", GOOD.encode())}, headers=P("s3"))) == "no_team", "an account with no team cannot submit")
os.environ["COMP_MAX_TEAM_SIZE"] = "2"
check(err(S3.post("/teams/join", json={"code": code}, headers=P("s3"))) == "team_full", "team size cap is enforced"); os.environ.pop("COMP_MAX_TEAM_SIZE")
for _ in range(10): S4.post("/teams/join", json={"code": "WRONGWRG"}, headers=P("s9"))
check(err(S4.post("/teams/join", json={"code": code}, headers=P("s9"))) == "too_many_attempts", "guessing join codes is throttled (10 wrong tries, then locked)")
n_teams = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
os.environ["COMP_MAX_TEAMS"] = str(n_teams)
check(err(S3.post("/teams", json={"name": "Overflow"}, headers=P("s3"))) == "teams_full", "total team cap is enforced"); os.environ.pop("COMP_MAX_TEAMS")
os.environ["ALLOWED_TENANT_ID"] = "Tenant-1"
check(err(S1.get("/me", headers=P("s1"))) == "wrong_tenant" and err(S1.get("/me", headers=P("s1", extra={"x-user-tenant": "other"}))) == "wrong_tenant",
      "with ALLOWED_TENANT_ID set, accounts from another tenant (or with no tenant header) are refused")
check(S1.get("/me", headers=P("s1", extra={"x-user-tenant": "TENANT-1"})).status_code == 200, "the allowed tenant works (case-insensitive)"); os.environ.pop("ALLOWED_TENANT_ID")
check(S3.post("/teams", json={"name": "Ghost"}, headers=P("s3", "s3@uni.edu")).status_code == 201, "another account creates a team that will never submit")
os.environ["EVENT_LOCK"] = "2021-01-01T00:00:00Z"
check(err(S4.post("/teams", json={"name": "Late"}, headers=P("s4"))) == "event_locked" and err(S4.post("/teams/join", json={"code": code}, headers=P("s4"))) == "event_locked", "no new teams or members after the lock")
os.environ["EVENT_LOCK"] = "2099-01-01T00:00:00Z"
ranked, problems = common.build_leaderboard(conn)
check("Ghost" not in [x[1] for x in ranked] and "Ghost" not in [p[0] for p in problems], "a self-created team that never submitted is left off the ranking")
check("Rocket Club" in [p[0] for p in problems], "a self-created team that submitted but has no final run blocks release (final-run not done)")
check(any(x[1] == "Delta" for x in ranked), "organiser-registered teams with no submission are still ranked (PnL 0, below the others)")
os.environ["SIGNUP_MODE"] = "invite"
check(err(S4.post("/teams", json={"name": "Nope"}, headers=P("s4"))) == "signup_closed", "SIGNUP_MODE=invite closes self sign-up"); os.environ["SIGNUP_MODE"] = "open"
cli("add-member", "pre@uni.edu", "--team", "Rocket Club")
check(client().get("/me", headers=P("s7", "pre@uni.edu")).json()["team"] == "Rocket Club", "an organiser-registered email still links to its team in open mode")
check(cli("new-code", "Rocket Club").stdout.split(": ")[-1].strip() != code and S2.get("/teams/mine", headers=P("s2")).json()["join_code"] != code, "new-code replaces a team's join code")
check(cli("move-member", "pre@uni.edu", "--team", "Ghost").stdout.strip() == "ok" and conn.execute("SELECT team FROM members WHERE email='pre@uni.edu'").fetchone()[0] == "Ghost", "move-member moves a person between teams")

# ---- migration from the earlier schema ------------------------------------------------------------
import sqlite3 as _sq
old = _sq.connect(f"{tmp}/old.db")
old.executescript("CREATE TABLE teams (name TEXT PRIMARY KEY COLLATE NOCASE, password_hash TEXT NOT NULL, created REAL NOT NULL);"
                  "CREATE TABLE members (email TEXT PRIMARY KEY COLLATE NOCASE, team TEXT NOT NULL, user_id TEXT UNIQUE);"
                  "INSERT INTO teams VALUES ('Old','x',1); INSERT INTO members VALUES ('old@uni.edu','Old','u-old');")
old.commit(); old.close()
mc = common.init_db(f"{tmp}/old.db")
check({"join_code", "source"} <= set(common._cols(mc, "teams")) and {"id", "display_name"} <= set(common._cols(mc, "members")), "a database from the earlier version is migrated in place")
check(tuple(mc.execute("SELECT email, team, user_id FROM members").fetchone()) == ("old@uni.edu", "Old", "u-old") and mc.execute("SELECT source FROM teams").fetchone()[0] == "organiser", "migration keeps existing teams and members")

print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED"); sys.exit(0 if ok else 1)
