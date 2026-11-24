"""
sandbox.py: TRUSTED side. SandboxedTrader quacks like a Trader (run(state)) but
forwards each tick to an untrusted child process and enforces limits:

  * per-tick wall-clock limit (the child is SIGKILLed on a miss, then restarted;
    only the traderData string survives, matching the documented contract)
  * first call of a run gets a larger allowance (cold imports / lazy init)
  * abort after too many timeouts or restarts
  * child runs as `nobody` when the parent is root, with rlimits on memory,
    CPU, processes, open files and file size
  * all responses are strictly validated; the parent never unpickles anything
"""
import json
import os
import resource
import select
import signal
import subprocess
import sys
import time

from protocol import MAX_LINE, decode_orders, encode_state

HERE = os.path.dirname(os.path.abspath(__file__))
NOBODY = 65534
AS_MB = int(os.environ.get("SANDBOX_AS_MB", "4096"))


class SubmissionError(Exception):
    """The submission could not be loaded (syntax error, disallowed import, no Trader)."""


class RunAborted(BaseException):
    """The run was stopped (too many timeouts/restarts, total time). BaseException so
    the engine's `except Exception` around trader.run can't swallow it."""


class ChildDied(Exception):
    pass


def _preexec(drop_privs):
    def fn():
        if drop_privs and os.geteuid() == 0:
            os.setgroups([])
            os.setgid(NOBODY)
            os.setuid(NOBODY)
        as_bytes = AS_MB * 1024 * 1024
        for res, lim in ((resource.RLIMIT_AS, as_bytes), (resource.RLIMIT_CPU, 900),
                         (resource.RLIMIT_NPROC, 64), (resource.RLIMIT_NOFILE, 256),
                         (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_CORE, 0)):
            try:
                resource.setrlimit(res, (lim, lim))
            except (ValueError, OSError):
                if sys.platform == "linux":      # on the grading server a failure must be loud
                    raise
    return fn


class SandboxedTrader:
    def __init__(self, submission_path, *, tick_limit=0.100, first_call_limit=2.0,
                 startup_limit=20.0, max_timeouts=25, max_restarts=25,
                 log_cap=20_000, drop_privs=True):
        self.path = submission_path
        self.tick_limit, self.first_call_limit = tick_limit, first_call_limit
        self.startup_limit, self.max_timeouts = startup_limit, max_timeouts
        self.max_restarts, self.log_cap = max_restarts, log_cap
        self.drop_privs = drop_privs
        self.proc = None
        self.buf = b""
        self.called = False
        self.timeouts = self.crashes = self.errors = self.restarts = self.invalid = 0
        self.logs, self._log_size, self._err_logged = [], 0, 0
        self.tick_times = []      # seconds, for ticks that completed in time

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        self._spawn()

    def close(self):
        self._kill()

    def _kill(self):
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            for f in (self.proc.stdin, self.proc.stdout):
                try:
                    f.close()
                except Exception:
                    pass
            try:
                self.proc.wait(timeout=2)
            except Exception:
                pass
            self.proc = None

    def _spawn(self):
        self._kill()
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent",
               "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
               "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
               "NUMEXPR_NUM_THREADS": "1"}
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "sandbox_child.py"), self.path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, env=env, cwd=os.path.dirname(os.path.abspath(self.path)),
            close_fds=True, start_new_session=True, preexec_fn=_preexec(self.drop_privs))
        self.buf = b""
        try:
            line = self._readline(time.monotonic() + self.startup_limit)
        except ChildDied:
            self._kill()
            raise SubmissionError("The submission process exited while loading "
                                  "(out of memory or crashed at import time).")
        if line is None:
            self._kill()
            raise SubmissionError(f"The submission took longer than {self.startup_limit:.0f}s to load.")
        msg = json.loads(line)
        if not msg.get("ready"):
            self._kill()
            raise SubmissionError(str(msg.get("error", "failed to load"))[:2000])

    # ---- IO --------------------------------------------------------------
    def _readline(self, deadline):
        fd = self.proc.stdout.fileno()
        while b"\n" not in self.buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(fd, 65536)
            if not chunk:
                raise ChildDied()
            self.buf += chunk
            if len(self.buf) > MAX_LINE and b"\n" not in self.buf:
                raise ChildDied()
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def _send(self, data):
        fd = self.proc.stdin.fileno()
        while data:
            n = os.write(fd, data)
            data = data[n:]

    def _log(self, ts, text):
        if not text or self._log_size >= self.log_cap:
            return
        entry = f"[t={ts}] {text}"
        self.logs.append(entry)
        self._log_size += len(entry)
        if self._log_size >= self.log_cap:
            self.logs.append("[log truncated]")

    # ---- the Trader interface ---------------------------------------------
    def run(self, state):
        prev_td = state.trader_data
        empty = ({}, 0, prev_td)
        if self.proc is None or self.proc.poll() is not None:
            self.restarts += 1
            if self.restarts > self.max_restarts:
                raise RunAborted("the submission process had to be restarted too many times")
            try:
                self._spawn()
            except SubmissionError as e:
                raise RunAborted(f"the submission could not be restarted: {e}")

        limit = self.tick_limit if self.called else self.first_call_limit
        self.called = True
        t0 = time.monotonic()
        try:
            self._send((json.dumps(encode_state(state)) + "\n").encode())
            line = self._readline(t0 + limit)
        except (ChildDied, BrokenPipeError, OSError):
            self.crashes += 1
            self._log(state.timestamp, "process exited unexpectedly (crash or out of memory)")
            self._kill()
            return empty

        if line is None:
            self.timeouts += 1
            self._log(state.timestamp, f"time limit exceeded ({self.tick_limit * 1000:.0f} ms); orders dropped")
            self._kill()
            if self.timeouts > self.max_timeouts:
                raise RunAborted(f"too many time-limit violations (>{self.max_timeouts})")
            return empty

        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                raise ValueError("bad message")
            self._log(state.timestamp, str(msg.get("log", ""))[:1100])
            if "err" in msg:
                self.errors += 1
                if self._err_logged < 5:
                    self._err_logged += 1
                    self._log(state.timestamp, "ERROR in run():\n" + str(msg["err"])[:1000])
                return empty
            td = msg["td"]
            if not isinstance(td, str):
                raise ValueError("traderData not a string")
            orders = decode_orders(msg["orders"], set(state.listings))
            self.tick_times.append(time.monotonic() - t0)
            return orders, 0, td
        except (ValueError, KeyError, TypeError):
            self.invalid += 1
            self._log(state.timestamp, "invalid response from submission; orders dropped")
            return empty
