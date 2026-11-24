"""
app.py: the competition API (FastAPI). Run:  uvicorn app:app --host 0.0.0.0 --port 8000

Environment:
  COMP_DB, COMP_UPLOADS     database file / folder for submitted code
  BACKEND_JWT_PUBLIC_KEY    Ed25519 public key (PEM; newlines may be written as \n). The website signs a short-lived
                            JWT per request (Authorization: Bearer, alg EdDSA, iss qfc-web, aud qfc-backtest); the
                            `team` claim is the team. Sign-in, teams and the tenant check all live on the website.
  EVENT_START, EVENT_LOCK   ISO-8601 UTC, defaults match the real event
  COMP_COOLDOWN_S, COMP_DAILY_LIMIT, COMP_MAX_BYTES   rule overrides (defaults 300, 15, 102400)
"""
import ast
import asyncio
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
import jwt

import common
import worker

RESULT_FIELDS = ("pnl", "aborted", "message", "pnl_by_product", "max_drawdown", "limit_violations",
                 "timeouts", "crashes", "errors", "restarts", "invalid", "mean_tick_ms", "p99_tick_ms",
                 "max_tick_ms", "ticks", "elapsed_s", "pnl_curve", "logs")

app = FastAPI(title="Quant Club Trading Competition API")


def env_int(name, default):
    return int(os.environ.get(name, default))


def db_path():
    return os.environ.get("COMP_DB", "jobs.db")


_initialised = set()


@contextmanager
def db():
    path = db_path()
    if path not in _initialised:          # create tables on first use of this database file
        common.init_db(path).close()
        _initialised.add(path)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---- errors ------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status, code, message, **extra):
        self.status, self.code, self.message, self.extra = status, code, message, extra


@app.exception_handler(ApiError)
async def _api_error(request, e):
    return JSONResponse(status_code=e.status, content={"error": e.code, "message": e.message, **e.extra})


@app.exception_handler(RequestValidationError)
async def _validation_error(request, e):
    return JSONResponse(status_code=400, content={"error": "bad_request", "message": "Invalid request."})


# ---- guards ---------------------------------------------------------------------
@app.middleware("http")
async def guard(request: Request, call_next):
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > 4 * env_int("COMP_MAX_BYTES", 102400):
        return JSONResponse(status_code=413, content={"error": "too_large", "message": "Request body too large."})
    return await call_next(request)


JWT_ISSUER, JWT_AUDIENCE = "qfc-web", "qfc-backtest"
_jwt_key = None


def jwt_public_key():
    global _jwt_key
    if _jwt_key is None:
        pem = os.environ.get("BACKEND_JWT_PUBLIC_KEY", "").replace("\\n", "\n").strip()
        if not pem:
            raise ApiError(500, "misconfigured", "Server misconfigured: BACKEND_JWT_PUBLIC_KEY is not set.")
        _jwt_key = pem
    return _jwt_key


def current_team(request: Request):
    """The team named by the website's signed token: the only auth in this service. The website does sign-in,
    teams, the tenant check and the standings; we only check the signature (EdDSA, pinned), issuer, audience and
    expiry. Organiser operations are manage.py commands on the VM, not HTTP endpoints."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise ApiError(401, "not_logged_in", "Please sign in.")
    try:
        claims = jwt.decode(auth[7:], jwt_public_key(), algorithms=["EdDSA"], audience=JWT_AUDIENCE, issuer=JWT_ISSUER,
                            options={"require": ["exp", "sub", "team"]}, leeway=10)
    except jwt.PyJWTError:
        raise ApiError(401, "not_logged_in", "Please sign in.")
    team = claims.get("team")
    if not isinstance(team, str) or not 0 < len(team) <= 64:
        raise ApiError(401, "not_logged_in", "Please sign in.")
    return team


# ---- public ----------------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/event")
def event():
    start, lock = common.event_times()
    return {"state": common.event_state(), "start": common.iso(start), "lock": common.iso(lock),
            "now": common.iso(time.time()),
            "limits": {"max_bytes": env_int("COMP_MAX_BYTES", 102400),
                       "cooldown_seconds": env_int("COMP_COOLDOWN_S", 300),
                       "daily_limit": env_int("COMP_DAILY_LIMIT", 15)}}


# ---- submissions -------------------------------------------------------------------
def summarize(row, c=None):
    d = {"id": row["id"], "created": common.iso(row["created"]), "status": row["status"]}
    if row["result_json"]:
        res = json.loads(row["result_json"])
        d["pnl"] = res.get("pnl")
        d["aborted"] = bool(res.get("aborted"))
        d["message"] = (res.get("message") or "")[:200]
    if row["status"] == "queued" and c is not None:
        d["queue_position"] = c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND id<?", (row["id"],)).fetchone()[0] + 1
    return d


@app.post("/submissions", status_code=202)
async def submit(file: UploadFile = File(...), team: str = Depends(current_team)):
    state = common.event_state()
    if state == "before":
        raise ApiError(403, "event_not_open", "Submissions are not open yet.")
    if state == "locked":
        raise ApiError(403, "event_locked", "Submissions are locked.")
    max_bytes = env_int("COMP_MAX_BYTES", 102400)
    if not (file.filename or "").lower().endswith(".py"):
        raise ApiError(400, "invalid_file", "Upload a single .py file.")
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ApiError(413, "too_large", f"File is larger than {max_bytes // 1024} KB.")
    try:
        source = data.decode("utf-8")
        ast.parse(source)
    except UnicodeDecodeError:
        raise ApiError(400, "invalid_file", "File must be UTF-8 text.")
    except SyntaxError as e:
        raise ApiError(400, "syntax_error", f"Syntax error on line {e.lineno}: {e.msg}")

    updir = os.path.join(os.environ.get("COMP_UPLOADS", "uploads"), "".join(ch for ch in team if ch.isalnum() or ch in "-_") or "team")
    os.makedirs(updir, exist_ok=True)
    path = os.path.join(updir, uuid.uuid4().hex + ".py")
    with open(path, "wb") as f:
        f.write(data)

    now, cooldown, daily = time.time(), env_int("COMP_COOLDOWN_S", 300), env_int("COMP_DAILY_LIMIT", 15)
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            if c.execute("SELECT COUNT(*) FROM jobs WHERE team=? AND kind='slice' AND status IN ('queued','running')", (team,)).fetchone()[0]:
                raise ApiError(409, "active_job", "You already have a submission running. Wait for it to finish.")
            last = c.execute("SELECT MAX(created) FROM jobs WHERE team=? AND kind='slice'", (team,)).fetchone()[0]
            if last and now - last < cooldown:
                wait = int(cooldown - (now - last)) + 1
                raise ApiError(429, "cooldown", f"Please wait {wait} s before submitting again.", retry_after_seconds=wait)
            if c.execute("SELECT COUNT(*) FROM jobs WHERE team=? AND kind='slice' AND created>?", (team, now - 86400)).fetchone()[0] >= daily:
                raise ApiError(429, "daily_limit", f"Daily limit of {daily} submissions reached.")
            job_id = worker.enqueue(c, team, "slice", path)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            os.remove(path)
            raise
    return {"id": job_id, "status": "queued", "created": common.iso(now)}


@app.get("/submissions")
def list_submissions(team: str = Depends(current_team)):
    with db() as c:
        rows = c.execute("SELECT * FROM jobs WHERE team=? AND kind='slice' ORDER BY id DESC LIMIT 100", (team,)).fetchall()
        return {"submissions": [summarize(r, c) for r in rows]}


def submission_detail(sid, team):
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=? AND team=? AND kind='slice'", (sid, team)).fetchone()
        if not row:
            raise ApiError(404, "not_found", "Submission not found.")
        d = summarize(row, c)
    if row["result_json"]:
        res = json.loads(row["result_json"])
        d["result"] = {k: res[k] for k in RESULT_FIELDS if k in res}
    return d


@app.get("/submissions/{sid}")
def get_submission(sid: int, team: str = Depends(current_team)):
    return submission_detail(sid, team)


@app.get("/submissions/{sid}/events")
async def submission_events(sid: int, request: Request, team: str = Depends(current_team)):
    """Server-sent events: `status` (the list fields) whenever the status or queue position changes, then one `done`
    (the full detail, with `result`) when the job finishes, then the stream closes."""
    first = await asyncio.to_thread(submission_detail, sid, team)     # 404 before the stream starts

    async def stream():
        yield "retry: 2000\n\n"
        d, last, quiet = first, None, 0
        while True:
            if d["status"] in ("done", "error"):
                yield f"event: done\ndata: {json.dumps(d)}\n\n"
                return
            key = (d["status"], d.get("queue_position"))
            if key != last:
                yield f"event: status\ndata: {json.dumps(d)}\n\n"
                last, quiet = key, 0
            else:
                quiet += 1
                if quiet % 30 == 0:
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)
            if await request.is_disconnected():
                return
            d = await asyncio.to_thread(submission_detail, sid, team)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
