"""
manage.py: organiser CLI, run on the VM.  Uses COMP_DB (default jobs.db).
Teams, sign-in and standings live on the website; this only operates the job queue.

  python3 manage.py queue                  # job counts by status, and the 50 most recent jobs
  python3 manage.py rerun ID               # re-queue a finished job (e.g. after an internal grading error)
  python3 manage.py final-run [--force]    # queue a full-data job for each team's last submission before the lock
"""
import argparse
import os
import sys

import common


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["queue", "rerun", "final-run"])
    ap.add_argument("id", nargs="?", type=int)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    conn = common.init_db(os.environ.get("COMP_DB", "jobs.db"))

    if a.cmd == "queue":
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        print("counts:", counts or "(no jobs)")
        for r in conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 50"):
            print(f"{r['id']:>6}  {r['status']:8s} {r['kind']:5s} attempts {r['attempts']}  {common.iso(r['created'])}  {r['team']}")
    elif a.cmd == "rerun":
        if a.id is None:
            sys.exit("Usage: manage.py rerun ID")
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (a.id,)).fetchone()
        if not row:
            sys.exit("No such job.")
        if row["status"] not in ("done", "error"):
            sys.exit("Job is still queued or running.")
        conn.execute("UPDATE jobs SET status='queued', attempts=0, started=NULL, finished=NULL, result_json=NULL WHERE id=?", (a.id,))
        print("ok")
    elif a.cmd == "final-run":
        out = common.enqueue_final_runs(conn, force=a.force)
        print("queued:", out["queued"])
        print("already has a full run (use --force to redo):", out["skipped"])


if __name__ == "__main__":
    main()
