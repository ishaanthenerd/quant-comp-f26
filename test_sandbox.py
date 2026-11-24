"""
Attack / correctness suite for the sandbox. Run as root:  python test_sandbox.py
Stages the app with the SAME permissions the Dockerfile uses, then runs hostile
and broken traders through run_job.py and checks what happens.
"""
import json, os, shutil, subprocess, sys, tempfile, time

SRC = os.path.dirname(os.path.abspath(__file__))
STAGE = "/tmp/app_stage"
SUBS = "/tmp/subs_stage"
SECRET_FILE = "/tmp/hidden_secret.txt"

PUBLIC = ["datamodel.py", "protocol.py", "sandbox_child.py"]
PRIVATE = ["engine.py", "bots.py", "products.py", "scenario.py", "sandbox.py", "run_job.py"]


def stage():
    shutil.rmtree(STAGE, ignore_errors=True); shutil.rmtree(SUBS, ignore_errors=True)
    os.makedirs(STAGE); os.makedirs(SUBS)
    os.chmod(STAGE, 0o755); os.chmod(SUBS, 0o755)
    for f in PUBLIC + PRIVATE:
        shutil.copy(os.path.join(SRC, f), STAGE)
        os.chmod(os.path.join(STAGE, f), 0o644 if f in PUBLIC else 0o600)
    with open(SECRET_FILE, "w") as f:
        f.write("THE-HIDDEN-SEED")
    os.chmod(SECRET_FILE, 0o600)


def run(name, code, kind="slice"):
    path = os.path.join(SUBS, name + ".py")
    with open(path, "w") as f:
        f.write(code)
    os.chmod(path, 0o644)
    out = os.path.join(SUBS, name + ".json")
    env = dict(os.environ, COMP_SECRET_SEED="test-secret", HIDDEN_CANARY="CANARY-IN-ENGINE-ENV",
               SANDBOX_AS_MB=os.environ.get("TEST_AS_MB", "1536"))
    t = time.time()
    p = subprocess.run([sys.executable, os.path.join(STAGE, "run_job.py"), "--submission", path,
                        "--kind", kind, "--out", out], capture_output=True, text=True, env=env, timeout=600)
    with open(out) as f:
        r = json.load(f)
    r["_wall"] = round(time.time() - t, 1)
    r["_stderr"] = p.stderr
    return r


GOOD = '''
import math
from datamodel import Order
class Trader:
    def run(self, state):
        out = {}
        for sym, od in state.order_depths.items():
            m = od.mid_price()
            if m is None: continue
            pos = state.position.get(sym, 0)
            out[sym] = []
            if pos < 40: out[sym].append(Order(sym, math.floor(m), 2))
            if pos > -40: out[sym].append(Order(sym, math.ceil(m), -2))
        return out, 0, state.trader_data
'''

CASES = {
 "do_nothing": "class Trader:\n    def run(self, s): return {}, 0, ''\n",
 "import_os": "import os\nclass Trader:\n    def run(self, s): return {}, 0, ''\n",
 "import_subprocess_in_run": "class Trader:\n    def run(self, s):\n        import subprocess\n        return {}, 0, ''\n",
 "syntax_error": "class Trader\n    pass\n",
 "no_trader_class": "x = 1\n",
 "infinite_loop": "class Trader:\n    def run(self, s):\n        while True: pass\n",
 "too_slow": "class Trader:\n    def run(self, s):\n        t = 0\n        for i in range(8_000_000): t += i\n        return {}, 0, ''\n",
 "memory_bomb": "class Trader:\n    def __init__(self): self.h = []\n    def run(self, s):\n        for _ in range(50): self.h.append(b'x' * 10**8)\n        return {}, 0, ''\n",
 "print_flood": "class Trader:\n    def run(self, s):\n        print('x' * 10**6)\n        return {}, 0, ''\n",
 "exit_call": "class Trader:\n    def run(self, s):\n        exit()\n",
 "spoofed_globals": "class Trader:\n    def run(self, s):\n        __import__('os', {'__name__': 'pandas'})\n        return {}, 0, ''\n",
 "pandas_import": "import pandas as pd\nimport numpy as np\nclass Trader:\n    def run(self, s): return {}, 0, ''\n",
 "float_price": "from datamodel import Order\nclass Trader:\n    def run(self, s):\n        return {'COMET_PILLS': [Order('COMET_PILLS', 1000.5, 1)]}, 0, ''\n",
 "over_limit": "from datamodel import Order\nclass Trader:\n    def run(self, s):\n        return {'COMET_PILLS': [Order('COMET_PILLS', 10**6, 999)]}, 0, ''\n",
 "huge_traderdata": "class Trader:\n    def run(self, s): return {}, 0, 'x' * 10**6\n",
 "unknown_symbol": "from datamodel import Order\nclass Trader:\n    def run(self, s):\n        return {'FAKE': [Order('FAKE', 1, 1)]}, 0, ''\n",
 # reads: all of these must fail with PermissionError / FileNotFoundError
 "read_secrets": '''
class Trader:
    def __init__(self): self.done = False
    def run(self, s):
        if self.done: return {}, 0, ''
        self.done = True
        ppid = open('/proc/self/stat').read().split()[3]
        for label, path in [('secretfile', '/tmp/hidden_secret.txt'),
                            ('bots_src', '/tmp/app_stage/bots.py'),
                            ('scenario_src', '/tmp/app_stage/scenario.py'),
                            ('parent_environ', '/proc/%s/environ' % ppid),
                            ('parent_mem', '/proc/%s/mem' % ppid)]:
            try:
                open(path, 'rb').read(10)
                print('READ_OK', label)
            except Exception as e:
                print('BLOCKED', label, type(e).__name__)
        return {}, 0, ''
''',
 # import-allowlist bypass via object.__subclasses__(): the allowlist is NOT the security boundary
 "bypass_attempt": '''
class Trader:
    def __init__(self): self.done = False
    def run(self, s):
        if self.done: return {}, 0, ''
        self.done = True
        cw = [c for c in object.__subclasses__() if c.__name__ == 'catch_warnings'][0]
        imp = cw()._module.__builtins__['__import__']
        try:
            imp('os'); print('PLAIN_BYPASS_OK')
        except ImportError:
            print('plain bypass blocked by allowlist')
        os = imp.__closure__[0].cell_contents('os')   # dig the real import out of the guard
        print('BYPASS_IMPORT_OK uid=', os.getuid())
        for label, fn in [('kill_parent', lambda: os.kill(os.getppid(), 9)),
                          ('read_secret', lambda: open('/tmp/hidden_secret.txt').read()),
                          ('setuid_root', lambda: os.setuid(0)),
                          ('write_app', lambda: open('/tmp/app_stage/evil.py', 'w').write('x'))]:
            try:
                fn(); print('DID', label)
            except Exception as e:
                print('BLOCKED', label, type(e).__name__)
        return {}, 0, ''
''',
}


def main():
    stage()
    results = {}
    print(f"{'case':26s} {'status':7s} {'pnl':>9s} {'t/o':>4s} {'err':>5s} {'crash':>5s} {'rst':>4s} {'wall':>6s}  message")
    good = run("good", GOOD)
    results["good"] = good
    names = ["good"] + list(CASES)
    results.update({n: run(n, CASES[n]) for n in CASES})
    for n in names:
        r = results[n]
        print(f"{n:26s} {r['status']:7s} {r.get('pnl', float('nan')):9.1f} {r['timeouts']:4d} "
              f"{r['errors']:5d} {r['crashes']:5d} {r['restarts']:4d} {r['_wall']:6.1f}  "
              f"{r['message'].splitlines()[-1][:70] if r['message'] else ''}")

    ok = True
    def check(cond, msg):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + msg)
        ok &= bool(cond)

    print()
    check(results["good"]["status"] == "ok" and results["good"]["pnl"] > 0, "good trader scores a profit")
    check(run("good2", GOOD)["pnl"] == results["good"]["pnl"], "same code twice gives identical PnL (deterministic)")
    check(results["do_nothing"]["pnl"] == 0, "do-nothing scores exactly 0")
    check("'os' is not allowed" in results["import_os"]["message"], "import os rejected at load")
    check(results["import_subprocess_in_run"]["errors"] > 0 and "not allowed" in results["import_subprocess_in_run"]["logs"],
          "import inside run() rejected, error shown to team")
    check(results["spoofed_globals"]["errors"] > 0 and "not allowed" in results["spoofed_globals"]["logs"],
          "__import__('os') with spoofed globals is still blocked")
    check(results["pandas_import"]["status"] == "ok", "pandas + numpy import cleanly inside the sandbox")
    check(results["syntax_error"]["status"] == "error", "syntax error reported")
    check(results["no_trader_class"]["status"] == "error", "missing Trader class reported")
    check(results["infinite_loop"]["status"] == "error" and "time-limit" in results["infinite_loop"]["message"],
          "infinite loop aborts the run")
    check(results["infinite_loop"]["_wall"] < 90, f"infinite loop contained quickly ({results['infinite_loop']['_wall']}s)")
    check(results["too_slow"]["timeouts"] > 0, "over-slow ticks are timed out")
    mb = results["memory_bomb"]
    check(mb["errors"] + mb["crashes"] + mb["timeouts"] > 0 and mb["_wall"] < 120,
          "memory bomb contained (MemoryError, crash or timeout-abort; run finishes quickly)")
    check(len(results["print_flood"]["logs"]) <= 20_100, "print flood capped in logs")
    check(results["exit_call"]["status"] == "ok" and results["exit_call"]["errors"] > 0, "exit() only errors that tick")
    check("must be int" in results["float_price"]["logs"], "float price gives a clear error")
    check(results["over_limit"]["limit_violations"] > 0, "over-limit orders rejected by engine")
    check(results["huge_traderdata"]["errors"] > 0 and results["huge_traderdata"]["restarts"] == 0,
          "oversized traderData errors without killing the process")
    check(results["unknown_symbol"]["invalid"] + results["unknown_symbol"]["errors"] > 0, "unknown symbol rejected")
    logs = results["read_secrets"]["logs"]
    check("READ_OK" not in logs and logs.count("BLOCKED") == 5, "participant cannot read secret file, engine source, or parent /proc")
    logs = results["bypass_attempt"]["logs"]
    check("uid= 65534" in logs, "child runs as uid 65534 (nobody) even after an import-allowlist bypass")
    check("DID" not in logs and logs.count("BLOCKED") == 4, "after bypass: cannot kill parent, read secret, setuid(0), or write /tmp/app_stage")
    check(not os.path.exists("/tmp/app_stage/evil.py"), "no file was written into the app dir")
    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
