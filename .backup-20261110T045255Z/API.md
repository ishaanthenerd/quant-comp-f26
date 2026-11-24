# Competition API: reference for the website

The backend is a standalone submission runner: it takes a `.py` trader, queues it, runs it in the sandbox and reports
the result. **Sign-in, teams, the tenant check and the standings all live on the website** (Next.js on Vercel).
It is served over HTTPS at `https://136-114-102-181.sslip.io` (Caddy in front of uvicorn, which listens on
127.0.0.1:8000 only). The browser never calls it directly: the website's server does.

## Auth

`/health` and `/event` are public. Every `/submissions` route needs

```
Authorization: Bearer <JWT>
```

signed by the website with its **Ed25519 private key** (`alg: EdDSA`). The backend has only the public key
(`BACKEND_JWT_PUBLIC_KEY` in `/etc/quant-comp.env`) and checks:

| Claim | Value |
|---|---|
| `iss` | `qfc-web` |
| `aud` | `qfc-backtest` |
| `exp` | required; the website issues 2-minute tokens (10 s clock leeway) |
| `sub` | the website's user id (informational) |
| `team` | the website's team id (UUID). Jobs, limits and uploads are keyed by it |

Anything else gets `401 not_logged_in`. There is no other auth: no admin endpoints, sessions or passwords.

## Errors

Every error has the same shape. Show `message` to the user and branch on `error`.

```json
{ "error": "cooldown", "message": "Please wait 212 s before submitting again.", "retry_after_seconds": 212 }
```

| HTTP | `error` | When |
|---|---|---|
| 400 | `bad_request` | Malformed JSON or missing fields |
| 400 | `invalid_file` | Not a `.py` file, or not UTF-8 |
| 401 | `not_logged_in` | Missing, invalid or expired JWT |
| 400 | `syntax_error` | The file has a Python syntax error (message includes the line number). **Not counted against the limits** |
| 403 | `event_not_open` / `event_locked` | Before the start / after the lock |
| 404 | `not_found` | Unknown submission, or one that belongs to another team |
| 409 | `active_job` | The team already has a submission queued or running |
| 413 | `too_large` | File over 100 KB |
| 429 | `cooldown` | Submitted too soon (`retry_after_seconds` included) |
| 429 | `daily_limit` | 15 submissions in the last 24 hours |

## Endpoints

### `GET /event` (no login needed)
Use it for the countdown, the open/locked banner, and the limits shown on the upload page.
```json
{ "state": "open",                      // "before" | "open" | "locked"
  "start": "2026-11-11T02:00:00Z", "lock": "2026-11-17T02:00:00Z", "now": "2026-11-12T01:00:00Z",
  "limits": { "max_bytes": 102400, "cooldown_seconds": 300, "daily_limit": 15 } }
```
Times are UTC (8pm Central is 02:00 UTC the next day). Format them in the viewer's local time, and show Central explicitly.

### `POST /submissions`  (multipart form, field name `file`)
```js
const fd = new FormData(); fd.append("file", fileInput.files[0]);
const res = await fetch(`${BACKEND}/submissions`, { method: "POST", body: fd, headers: { Authorization: `Bearer ${jwt}` } });   // do NOT set content-type yourself
```
Success `202`: `{"id": 12, "status": "queued", "created": "2026-11-12T01:02:03Z"}`.

### `GET /submissions`  ->  the team's own submissions, newest first
```json
{ "submissions": [
  { "id": 12, "created": "…", "status": "queued", "queue_position": 2 },
  { "id": 11, "created": "…", "status": "done",  "pnl": 697.67, "aborted": false, "message": "" },
  { "id": 10, "created": "…", "status": "error", "pnl": 0.0,    "aborted": true,
    "message": "Could not load submission:\n  line 1, in <module>: import os\nImportError: import of 'os' is not allowed" } ] }
```
`status` is `queued`, `running`, `done` or `error`. To follow a running job, listen to `GET /submissions/{id}/events` (below) rather than polling.
`error` with `aborted: true` means the team's code failed or was stopped (it scores PnL 0). It is not a site error.

### `GET /submissions/{id}`  ->  the list fields plus `result` once finished
```json
{ "id": 11, "status": "done", "pnl": 697.67, "aborted": false, "message": "", "created": "…",
  "result": {
    "pnl": 697.67, "pnl_by_product": { "ALPHA": 120.5, "BETA": 300.2 }, "max_drawdown": 143.9,
    "limit_violations": 0, "timeouts": 0, "crashes": 0, "errors": 0, "restarts": 0, "invalid": 0,
    "mean_tick_ms": 0.18, "p99_tick_ms": 0.35, "max_tick_ms": 2.34, "ticks": 1000, "elapsed_s": 0.34,
    "pnl_curve": [0.0, 3.5, 7.0],       // about 200 points of cumulative PnL: draw this as a line chart
    "logs": "[t=0] hello from print()\n..." } }
```
Suggested display: PnL and max drawdown as headline numbers, the curve as a chart, per-product PnL as a small table,
`logs` and `message` in a monospace box, and a warning if `p99_tick_ms` is near 100 (the per-tick limit).
`timeouts`, `errors`, `limit_violations` deserve a visible warning when above zero.

### `GET /submissions/{id}/events`  ->  server-sent events (instead of polling)
`text/event-stream`. Sends `event: status` with the list fields (above) whenever the status or queue position changes,
then one `event: done` with the full `GET /submissions/{id}` body (including `result`), then closes. A `retry: 2000`
line asks clients to reconnect after 2 s if the connection drops; `: keep-alive` comments are sent while nothing changes.
```
event: status
data: {"id": 12, "created": "…", "status": "queued", "queue_position": 2}

event: done
data: {"id": 12, "status": "done", "pnl": 697.67, "aborted": false, "message": "", "created": "…", "result": {…}}
```

### Organiser operations
Not over HTTP. On the VM: `python3 manage.py queue | rerun ID | final-run [--force]` (see `BACKEND_SETUP.md`).
