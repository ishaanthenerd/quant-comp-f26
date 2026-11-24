"""
worker.py: SQLite job queue + worker loop.

    python worker.py init     --db jobs.db
    python worker.py enqueue  --db jobs.db --team A --kind slice --file sub.py
    python worker.py run      --db jobs.db --mode docker|local [--once]
    python worker.py status   --db jobs.db

The website only needs enqueue() and to read the `jobs` table.
Rules enforced here: FIFO, and at most one running job per team.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
JOB_TIMEOUT = {"slice": 1260, "full": 2520}
IMAGE = os.environ.get("COMP_IMAGE", "quant-comp-runner")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('slice','full')),
  submission_path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','error')),
  attempts INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL, started REAL, finished REAL,
  result_json TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, id);
"""


def connect(db):
    conn = sqlite3.connect(db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def enqueue(conn, team, kind, path):
    cur = conn.execute("INSERT INTO jobs(team,kind,submission_path,created) VALUES (?,?,?,?)",
                       (team, kind, os.path.abspath(path), time.time()))
    return cur.lastrowid


def claim(conn):
    """Atomically take the oldest queued job whose team has nothing running."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='queued' AND team NOT IN "
            "(SELECT team FROM jobs WHERE status='running') ORDER BY id LIMIT 1").fetchone()
        if row:
            conn.execute("UPDATE jobs SET status='running', started=?, attempts=attempts+1 WHERE id=?",
                         (time.time(), row["id"]))
        conn.execute("COMMIT")
        return row
    except Exception:
        conn.execute("ROLLBACK")
        raise


def requeue_stale(conn):
    """Jobs left 'running' by a crashed worker: retry once, then fail."""
    now = time.time()
    for r in conn.execute("SELECT * FROM jobs WHERE status='running'").fetchall():
        if now - r["started"] > JOB_TIMEOUT[r["kind"]] + 120:
            if r["attempts"] < 2:
                conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (r["id"],))
            else:
                finish(conn, r["id"], "error", {"status": "error", "message": "Grading worker failed repeatedly."})


def finish(conn, job_id, status, result):
    conn.execute("UPDATE jobs SET status=?, finished=?, result_json=? WHERE id=?",
                 (status, time.time(), json.dumps(result), job_id))


def docker_cmd(name, sub, kind):
    if not os.environ.get("COMP_SECRET_SEED"):
        raise RuntimeError("COMP_SECRET_SEED must be set (non-empty) before running jobs in docker mode")
    return ["docker", "run", "--rm", "--name", name,
            "--network", "none", "--memory", "2g", "--memory-swap", "2g", "--cpus", "1",
            "--pids-limit", "128", "--read-only",
            "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID", "--cap-add", "KILL",
            "--security-opt", "no-new-privileges",
            "-e", f"COMP_SECRET_SEED={os.environ['COMP_SECRET_SEED']}",
            "-v", f"{sub}:/work/submission.py:ro",
            IMAGE, "--submission", "/work/submission.py", "--kind", kind, "--out", "-"]


def _last_json_line(text):
    for line in reversed((text or "").strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


INTERNAL = {"status": "error", "internal": True,
            "message": "Internal grading error. The organisers have been notified."}


def run_job(row, mode):
    work = tempfile.mkdtemp(prefix=f"job{row['id']}-")
    os.chmod(work, 0o755)
    sub = os.path.join(work, "submission.py")
    shutil.copyfile(row["submission_path"], sub)
    os.chmod(sub, 0o644)
    timeout = JOB_TIMEOUT[row["kind"]]
    name = f"comp-job-{row['id']}-{int(time.time())}"
    try:
        if mode == "docker":
            cmd = docker_cmd(name, sub, row["kind"])
            out = None
        else:
            out = os.path.join(work, "result.json")
            cmd = [sys.executable, os.path.join(HERE, "run_job.py"), "--submission", sub,
                   "--kind", row["kind"], "--out", out]
        try:
            p = subprocess.run(cmd, timeout=timeout, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            if mode == "docker":
                subprocess.run(["docker", "kill", name], capture_output=True)
            return "error", {"status": "error", "pnl": 0.0, "aborted": True,
                             "message": "Run exceeded the total time limit."}
        if p.returncode != 0:
            print(f"[job {row['id']}] runner exit {p.returncode}, stderr:\n{p.stderr[-2000:]}", file=sys.stderr)
        if mode == "docker":
            result = _last_json_line(p.stdout)
        elif os.path.exists(out):
            with open(out) as f:
                result = json.load(f)
        else:
            result = None
        if result is None:
            return "error", dict(INTERNAL)
        return ("done" if result.get("status") == "ok" else "error"), result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def loop(db, mode, once=False, poll=2.0):
    conn = connect(db)
    requeue_stale(conn)
    while True:
        row = claim(conn)
        if row is None:
            if once:
                return
            time.sleep(poll)
            continue
        status, result = run_job(row, mode)
        finish(conn, row["id"], status, result)
        print(f"[job {row['id']}] team={row['team']} kind={row['kind']} -> {status}")
        if once and conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] == 0:
            return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["init", "enqueue", "run", "status"])
    ap.add_argument("--db", default="jobs.db")
    ap.add_argument("--team")
    ap.add_argument("--kind", default="slice")
    ap.add_argument("--file")
    ap.add_argument("--mode", default="docker", choices=["docker", "local"])
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    conn = connect(a.db)
    if a.cmd == "enqueue":
        print(enqueue(conn, a.team, a.kind, a.file))
    elif a.cmd == "run":
        loop(a.db, a.mode, a.once)
    elif a.cmd == "status":
        for r in conn.execute("SELECT id,team,kind,status,attempts FROM jobs ORDER BY id"):
            print(dict(r))


if __name__ == "__main__":
    main()
