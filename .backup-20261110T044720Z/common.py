"""
common.py: shared by the API (app.py) and the organiser CLI (manage.py).
Config/event times, password hashing, app tables, final-run queuing and the leaderboard.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone

import worker
from ranking import rank

# ---- event window -----------------------------------------------------------
# 8pm Central on Nov 10 / Nov 16 2026 is CST (UTC-6): 02:00 UTC the next day.
DEFAULT_START = "2026-11-11T02:00:00Z"
DEFAULT_LOCK = "2026-11-17T02:00:00Z"


def _parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def event_times():
    return (_parse(os.environ.get("EVENT_START", DEFAULT_START)),
            _parse(os.environ.get("EVENT_LOCK", DEFAULT_LOCK)))


def event_state(now=None):
    now = time.time() if now is None else now
    start, lock = event_times()
    return "before" if now < start else ("locked" if now >= lock else "open")


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- passwords (stdlib scrypt, no extra dependency) --------------------------
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"      # no look-alike characters
_N, _R, _P = 2 ** 14, 8, 1


def new_password(n=10):
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def hash_password(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${h.hex()}"


def verify_password(pw, stored):
    try:
        _, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


DUMMY_HASH = hash_password("dummy-password")     # so unknown team names cost the same time as real ones

# ---- app tables (the jobs table lives in worker.py) -------------------------
APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (name TEXT PRIMARY KEY COLLATE NOCASE, password_hash TEXT, created REAL NOT NULL,
                                  join_code TEXT, source TEXT NOT NULL DEFAULT 'organiser');
CREATE TABLE IF NOT EXISTS members (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT COLLATE NOCASE UNIQUE,
                                    team TEXT NOT NULL, user_id TEXT UNIQUE, display_name TEXT);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, team TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_failures (team TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def init_db(path):
    conn = worker.connect(path)          # creates the jobs table
    conn.executescript(APP_SCHEMA)       # no-op for tables that already exist
    # migrate databases created by earlier versions of this code
    tcols = _cols(conn, "teams")
    if "join_code" not in tcols:
        conn.execute("ALTER TABLE teams ADD COLUMN join_code TEXT")
    if "source" not in tcols:
        conn.execute("ALTER TABLE teams ADD COLUMN source TEXT NOT NULL DEFAULT 'organiser'")
    if "id" not in _cols(conn, "members"):
        conn.execute("ALTER TABLE members RENAME TO members_old")
        conn.executescript(APP_SCHEMA)
        conn.execute("INSERT INTO members(email, team, user_id) SELECT email, team, user_id FROM members_old")
        conn.execute("DROP TABLE members_old")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS teams_join_code ON teams(join_code)")
    return conn


def new_join_code(conn):
    while True:
        code = "".join(secrets.choice(ALPHABET) for _ in range(8)).upper()
        if not conn.execute("SELECT 1 FROM teams WHERE join_code=?", (code,)).fetchone():
            return code


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))


# ---- final run + leaderboard -------------------------------------------------
def enqueue_final_runs(conn, lock_ts=None, force=False):
    """Queue a full-data job for each team's LAST slice submission made before the lock."""
    lock_ts = event_times()[1] if lock_ts is None else lock_ts
    out = {"queued": [], "no_submission": [], "skipped": [], "unranked": []}
    for tr in conn.execute("SELECT name, source FROM teams ORDER BY name").fetchall():
        t = tr["name"]
        last = conn.execute("SELECT * FROM jobs WHERE team=? AND kind='slice' AND created<=? "
                            "ORDER BY id DESC LIMIT 1", (t, lock_ts)).fetchone()
        if not last:
            out["unranked" if tr["source"] == "self" else "no_submission"].append(t)
            continue
        if not force and conn.execute("SELECT 1 FROM jobs WHERE team=? AND kind='full'", (t,)).fetchone():
            out["skipped"].append(t)
            continue
        worker.enqueue(conn, t, "full", last["submission_path"])
        out["queued"].append(t)
    return out


def build_leaderboard(conn):
    """Returns (ranked, problems). Ranked = ranking.rank() output; problems = teams that block publishing."""
    rows = conn.execute("SELECT j.* FROM jobs j JOIN (SELECT team, MAX(id) mid FROM jobs "
                        "WHERE kind='full' GROUP BY team) m ON j.id = m.mid").fetchall()
    results, problems = {}, []
    lock_ts = event_times()[1]
    have_full = {r["team"] for r in rows}
    for tr in conn.execute("SELECT name, source FROM teams ORDER BY name").fetchall():
        t = tr["name"]
        if t in have_full:
            continue
        if conn.execute("SELECT 1 FROM jobs WHERE team=? AND kind='slice' AND created<=?", (t, lock_ts)).fetchone():
            problems.append((t, "final run not queued: run manage.py final-run"))
        elif tr["source"] != "self":
            # a team the organisers registered that never submitted: PnL 0, ranked below every other zero-PnL team.
            # (Teams people created themselves and never submitted with are left off the ranking.)
            results[t] = {"status": "none", "pnl": 0.0, "max_drawdown": 0.0, "no_submission": True, "aborted": False}
    for r in rows:
        if r["status"] in ("queued", "running"):
            problems.append((r["team"], "full run not finished"))
            continue
        res = json.loads(r["result_json"] or "null")
        if not res or res.get("internal") or "pnl" not in res:
            problems.append((r["team"], "grading error: re-run needed"))
            continue
        results[r["team"]] = res
    return rank(results), problems
