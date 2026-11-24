"""
sandbox_child.py: runs INSIDE the untrusted process, as an unprivileged user.

It imports ONLY datamodel and protocol. It never imports the engine, bots or
scenario, and it is only ever sent TradingState objects, so it holds no hidden
data. The import allowlist below is a convenience/guardrail, NOT a security
boundary (Python-level restrictions can be bypassed). The real boundary is:
  * separate process + unprivileged uid (can't read root-only files or /proc of the engine)
  * resource limits set by the parent (memory, CPU, processes, file size)
  * in production, a Docker container with no network and a read-only filesystem.
"""
import builtins
import json
import linecache
import numbers
import os
import sys
import traceback
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ALLOWED_IMPORTS = frozenset({
    "datamodel", "typing", "math", "statistics", "collections", "json",
    "numpy", "pandas", "itertools", "functools", "heapq", "copy", "dataclasses",
})
PER_TICK_LOG = 1000


class Capture:
    """Replaces sys.stdout/stderr so participant prints can't touch the protocol pipe."""
    def __init__(self):
        self.parts, self.size = [], 0

    def write(self, s):
        if self.size < PER_TICK_LOG:
            s = str(s)[: PER_TICK_LOG - self.size]
            self.parts.append(s)
            self.size += len(s)
        return len(s)

    def flush(self):
        pass

    def take(self):
        out = "".join(self.parts)
        if self.size >= PER_TICK_LOG:
            out += " ...[truncated]"
        self.parts, self.size = [], 0
        return out


def main():
    path = sys.argv[1]
    rfd, wfd = os.dup(0), os.dup(1)
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    os.dup2(null, 1)
    cap = Capture()
    sys.stdout = sys.stderr = cap

    from protocol import MAX_ORDERS_PER_SYMBOL, MAX_TRADER_DATA, decode_state  # before guard

    def send(obj):
        data = (json.dumps(obj) + "\n").encode()
        while data:
            n = os.write(wfd, data)
            data = data[n:]

    def fmt(e):
        frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename == "submission.py"]
        lines = [f'  line {f.lineno}, in {f.name}: {f.line}' for f in frames[-3:]]
        return "\n".join(lines + [f"{type(e).__name__}: {str(e)[:300]}"])

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        # Judge the module that actually made the call (its frame), not the `globals`
        # argument: libraries like pandas call __import__('dateutil') with no globals,
        # and a participant could pass fake globals to spoof the check.
        caller = sys._getframe(1).f_globals.get("__name__")
        if caller == "_submission":
            if level != 0 or name.split(".")[0] not in ALLOWED_IMPORTS:
                raise ImportError(f"import of '{name.split('.')[0]}' is not allowed")
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = guarded_import
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        linecache.cache["submission.py"] = (len(src), None, src.splitlines(True), "submission.py")
        mod = types.ModuleType("_submission")
        exec(compile(src, "submission.py", "exec"), mod.__dict__)
        trader = mod.Trader()
    except BaseException as e:
        send({"ready": False, "error": fmt(e)})
        return
    send({"ready": True})

    def encode_orders(orders):
        if not isinstance(orders, dict):
            raise TypeError("run() must return a dict mapping symbol -> list of Order")
        out = {}
        for sym, lst in orders.items():
            if len(lst) > MAX_ORDERS_PER_SYMBOL:
                raise ValueError(f"too many orders for {sym} (max {MAX_ORDERS_PER_SYMBOL})")
            enc = []
            for o in lst:
                p, q = o.price, o.quantity
                for nm, v in (("price", p), ("quantity", q)):
                    if isinstance(v, bool) or not isinstance(v, numbers.Integral):
                        raise TypeError(f"Order {nm} must be an int, got {type(v).__name__}")
                if o.symbol != sym:
                    raise ValueError(f"Order symbol {o.symbol!r} filed under {sym!r}")
                enc.append([int(p), int(q)])
            out[str(sym)] = enc
        return out

    buf = b""
    while True:
        while b"\n" not in buf:
            chunk = os.read(rfd, 65536)
            if not chunk:
                return
            buf += chunk
        line, buf = buf.split(b"\n", 1)
        state = decode_state(json.loads(line))
        try:
            result = trader.run(state)
            orders, _conv, td = result
            if not isinstance(td, str):
                raise TypeError("traderData must be a str")
            if len(td) > MAX_TRADER_DATA:
                raise ValueError(f"traderData too large (max {MAX_TRADER_DATA} chars)")
            resp = {"ok": True, "orders": encode_orders(orders), "td": td, "log": cap.take()}
        except BaseException as e:   # includes SystemExit from exit()
            resp = {"err": fmt(e), "log": cap.take()}
        send(resp)


if __name__ == "__main__":
    main()
