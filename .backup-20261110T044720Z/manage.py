"""
manage.py: organiser CLI.  Uses COMP_DB (default jobs.db).

  python3 manage.py import-members members.csv          # CSV with columns email,team  (creates the teams too)
  python3 manage.py add-member EMAIL --team NAME         # add one Microsoft account to a team
  python3 manage.py remove-member EMAIL
  python3 manage.py unbind-member EMAIL                  # forget the linked Microsoft account (wrong account signed in first)
  python3 manage.py move-member EMAIL --team NAME        # put a person on a different team (people cannot leave teams themselves)
  python3 manage.py new-code NAME                        # replace a team's join code (if it leaked)
  python3 manage.py remove-team NAME                     # also removes its members; its past jobs stay
  python3 manage.py add-teams --count 12                 # password teams, for LOCAL TESTING (AUTH_MODE=password) only
  python3 manage.py add-teams --names "Alpha,Beta" [--no-password]
  python3 manage.py reset-password NAME
  python3 manage.py list-teams
  python3 manage.py final-run [--force]                  # queue full-data jobs for each team's last submission before the lock
  python3 manage.py ranking                              # print the ranking from finished full runs
  python3 manage.py release [--force] | unrelease        # show / hide the leaderboard on the website
"""
import argparse
import csv
import os
import sys

import common


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["add-teams", "reset-password", "list-teams", "final-run", "ranking", "release", "unrelease",
                                 "import-members", "add-member", "remove-member", "unbind-member", "remove-team", "new-code", "move-member"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--count", type=int)
    ap.add_argument("--prefix", default="team")
    ap.add_argument("--names")
    ap.add_argument("--out", default="credentials.csv")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--team")
    ap.add_argument("--no-password", action="store_true")
    a = ap.parse_args()
    conn = common.init_db(os.environ.get("COMP_DB", "jobs.db"))

    if a.cmd == "add-teams":
        names = [n.strip() for n in a.names.split(",")] if a.names else \
            [f"{a.prefix}{i:02d}" for i in range(1, (a.count or 0) + 1)]
        if not names or names == [""]:
            sys.exit("Give --count N or --names a,b,c")
        rows = []
        for n in names:
            if conn.execute("SELECT 1 FROM teams WHERE name=?", (n,)).fetchone():
                print(f"skip {n}: already exists")
                continue
            pw = None if a.no_password else common.new_password()
            conn.execute("INSERT INTO teams(name, password_hash, created) VALUES (?,?,strftime('%s','now'))",
                         (n, common.hash_password(pw) if pw else None))
            rows.append((n, pw or ""))
            if a.no_password:
                print(f"created team {n} (no password)")
        if rows:
            new = not os.path.exists(a.out)
            fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", newline="") as f:
                w = csv.writer(f, lineterminator="\n")
                if new:
                    w.writerow(["team", "password"])
                w.writerows(rows)
            print(f"Created {len(rows)} teams. Passwords are in {a.out} (only copy: hashes are stored in the database).")
    elif a.cmd == "import-members":
        if not a.name or not os.path.exists(a.name):
            sys.exit("Usage: manage.py import-members members.csv   (columns: email,team)")
        n_added = 0
        with open(a.name, newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                if len(row) < 2 or row[0].strip().lower() in ("", "email"):
                    continue
                email, team = row[0].strip().lower(), row[1].strip()
                if "@" not in email or not team:
                    print(f"skip bad row: {row}")
                    continue
                if conn.execute("SELECT 1 FROM teams WHERE name=?", (team,)).fetchone() is None:
                    conn.execute("INSERT INTO teams(name, password_hash, created) VALUES (?,NULL,strftime('%s','now'))", (team,))
                other = conn.execute("SELECT team FROM members WHERE email=?", (email,)).fetchone()
                if other and other["team"].lower() != team.lower():
                    print(f"skip {email}: already on team {other['team']} (remove-member first to move it)")
                    continue
                if not other:
                    conn.execute("INSERT INTO members(email, team) VALUES (?,?)", (email, team))
                    n_added += 1
        print(f"Added {n_added} members. They are linked to their Microsoft account the first time they sign in.")
    elif a.cmd == "add-member":
        if not a.name or not a.team or "@" not in a.name:
            sys.exit("Usage: manage.py add-member EMAIL --team NAME")
        if conn.execute("SELECT 1 FROM teams WHERE name=?", (a.team,)).fetchone() is None:
            conn.execute("INSERT INTO teams(name, password_hash, created) VALUES (?,NULL,strftime('%s','now'))", (a.team,))
        conn.execute("INSERT INTO members(email, team) VALUES (?,?) ON CONFLICT(email) DO UPDATE SET team=excluded.team, user_id=NULL",
                     (a.name.strip().lower(), a.team))
        print("ok")
    elif a.cmd in ("remove-member", "unbind-member"):
        em = (a.name or "").strip().lower()
        q = ("DELETE FROM members WHERE email=?" if a.cmd == "remove-member" else "UPDATE members SET user_id=NULL WHERE email=?")
        print("ok" if conn.execute(q, (em,)).rowcount else "no such member")
    elif a.cmd == "new-code":
        code = common.new_join_code(conn)
        print(f"{a.name}: {code}" if conn.execute("UPDATE teams SET join_code=? WHERE name=?", (code, a.name)).rowcount else "no such team")
    elif a.cmd == "move-member":
        if not a.name or not a.team or conn.execute("SELECT 1 FROM teams WHERE name=?", (a.team,)).fetchone() is None:
            sys.exit("Usage: manage.py move-member EMAIL --team EXISTING_TEAM")
        print("ok" if conn.execute("UPDATE members SET team=? WHERE email=?", (a.team, a.name.strip().lower())).rowcount else "no such member")
    elif a.cmd == "remove-team":
        conn.execute("DELETE FROM members WHERE team=?", (a.name,))
        conn.execute("DELETE FROM sessions WHERE team=?", (a.name,))
        print("ok" if conn.execute("DELETE FROM teams WHERE name=?", (a.name,)).rowcount else "no such team")
    elif a.cmd == "reset-password":
        pw = common.new_password()
        if conn.execute("UPDATE teams SET password_hash=? WHERE name=?", (common.hash_password(pw), a.name)).rowcount == 0:
            sys.exit("No such team")
        conn.execute("DELETE FROM sessions WHERE team=?", (a.name,))
        print(f"{a.name}: {pw}")
    elif a.cmd == "list-teams":
        for r in conn.execute("SELECT t.name, t.source, t.join_code, COUNT(j.id) n FROM teams t LEFT JOIN jobs j ON j.team=t.name AND j.kind='slice' "
                              "GROUP BY t.name ORDER BY t.name"):
            mem = conn.execute("SELECT email, display_name, user_id FROM members WHERE team=? ORDER BY id", (r["name"],)).fetchall()
            who = ", ".join(f"{x['email'] or x['display_name']}{'' if x['user_id'] else ' (not signed in yet)'}" for x in mem) or "none"
            print(f"{r['name']:22s} {r['n']:3d} submissions  [{r['source']}] code {r['join_code'] or '-':8s} members: {who}")
    elif a.cmd == "final-run":
        out = common.enqueue_final_runs(conn, force=a.force)
        print("queued:", out["queued"])
        print("already has a full run (use --force to redo):", out["skipped"])
        print("NO SUBMISSION (organiser-registered teams: scored 0, ranked below every other zero-PnL team):", out["no_submission"])
        print("self-created teams that never submitted (left off the ranking):", out["unranked"])
    elif a.cmd == "ranking":
        ranked, problems = common.build_leaderboard(conn)
        for rk, team, res in ranked:
            flag = "  (aborted: scored 0)" if res.get("aborted") else ("  (NO SUBMISSION: scored 0)" if res.get("no_submission") else "")
            print(f"{rk:>3}  {team:20s} pnl {res['pnl']:>12,.2f}   max drawdown {res.get('max_drawdown', 0):>10,.2f}{flag}")
        for team, why in problems:
            print(f"!!   {team}: {why}")
    elif a.cmd in ("release", "unrelease"):
        if a.cmd == "release" and not a.force:
            ranked, problems = common.build_leaderboard(conn)
            if problems or not ranked:
                sys.exit(f"Not releasing: incomplete results {problems or '(no full runs yet)'}. Use --force to override.")
        common.set_setting(conn, "results_released", "1" if a.cmd == "release" else "0")
        print("results_released =", a.cmd == "release")


if __name__ == "__main__":
    main()
