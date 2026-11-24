"""
common.py: shared by the API (app.py) and the organiser CLI (manage.py).
Event times, the database, and final-run queuing. Sign-in, teams and standings live on the website.
"""
import os
import time
from datetime import datetime, timezone

import worker

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


# ---- database ------------------------------------------------------------------
def init_db(path):
    """The jobs table (created by worker.connect) is the only table the runner needs."""
    return worker.connect(path)


# ---- final run -------------------------------------------------
def enqueue_final_runs(conn, lock_ts=None, force=False):
    """Queue a full-data job for each team's LAST slice submission made before the lock."""
    lock_ts = event_times()[1] if lock_ts is None else lock_ts
    out = {"queued": [], "skipped": []}
    teams = [r["team"] for r in conn.execute("SELECT DISTINCT team FROM jobs WHERE kind='slice' AND created<=? ORDER BY team",
                                             (lock_ts,))]
    for t in teams:
        last = conn.execute("SELECT * FROM jobs WHERE team=? AND kind='slice' AND created<=? "
                            "ORDER BY id DESC LIMIT 1", (t, lock_ts)).fetchone()
        if not force and conn.execute("SELECT 1 FROM jobs WHERE team=? AND kind='full'", (t,)).fetchone():
            out["skipped"].append(t)
            continue
        worker.enqueue(conn, t, "full", last["submission_path"])
        out["queued"].append(t)
    return out
