"""
Docker smoke test. Run on a machine with Docker, AFTER building the image:

    docker build -t quant-comp-runner .
    python3 test_docker.py            # security + correctness checks
    python3 test_docker.py --timing   # also time the pandas stress traders under the container's CPU cap

It runs submissions through the exact command the worker uses (worker.docker_cmd),
so it tests the real flags: no network, read-only filesystem, dropped capabilities,
memory/CPU/pids caps, uid drop and process kill.
"""
import json, os, secrets, shutil, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import worker

os.environ["COMP_SECRET_SEED"] = secrets.token_hex(16)   # throwaway secret for this test
HERE = os.path.dirname(os.path.abspath(__file__))


def run_case(name, code, kind="slice", timeout=300):
    work = tempfile.mkdtemp(prefix="dtest-")
    os.chmod(work, 0o755)
    sub = os.path.join(work, "submission.py")
    with open(sub, "w") as f:
        f.write(code)
    os.chmod(sub, 0o644)
    cname = f"comp-test-{name}-{int(time.time())}"
    t0 = time.time()
    try:
        p = subprocess.run(worker.docker_cmd(cname, sub, kind), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", cname], capture_output=True)
        return {"status": "HARNESS_TIMEOUT", "message": "", "logs": "", "_wall": round(time.time() - t0, 1), "_stderr": ""}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    r = worker._last_json_line(p.stdout) or {"status": "NO_RESULT", "message": "", "logs": ""}
    r["_wall"], r["_stderr"], r["_rc"] = round(time.time() - t0, 1), p.stderr, p.returncode
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

# Participant that defeats the import allowlist (dig the real import out of the guard), then probes the container.
PROBE = '''
class Trader:
    def __init__(self): self.done = False
    def run(self, s):
        if self.done: return {}, 0, ''
        self.done = True
        cw = [c for c in object.__subclasses__() if c.__name__ == 'catch_warnings'][0]
        real = cw()._module.__builtins__['__import__'].__closure__[0].cell_contents
        os = real('os'); socket = real('socket')
        print('UID', os.getuid())
        def attempt(label, fn):
            try:
                fn(); print('DID', label)
            except Exception as e:
                print('BLOCKED', label, type(e).__name__)
        def net():
            c = socket.socket(); c.settimeout(3); c.connect(('1.1.1.1', 53))
        attempt('network', net)
        attempt('read_engine_env', lambda: open('/proc/1/environ', 'rb').read(5))
        attempt('read_scenario_src', lambda: open('/app/scenario.py').read(5))
        attempt('read_bots_src', lambda: open('/app/bots.py').read(5))
        def write_file(path):
            with open(path, 'w') as f:      # 'with' so a failed flush on close is NOT silently ignored
                f.write('x')
        attempt('write_app', lambda: write_file('/app/evil.py'))
        attempt('write_tmp', lambda: write_file('/tmp/evil'))
        attempt('kill_engine', lambda: os.kill(1, 9))
        attempt('setuid_root', lambda: os.setuid(0))
        return {}, 0, ''
'''


def leftover_containers():
    p = subprocess.run(["docker", "ps", "-a", "--filter", "name=comp-test-", "-q"], capture_output=True, text=True)
    return [x for x in p.stdout.split() if x]


def main():
    if subprocess.run(["docker", "version"], capture_output=True).returncode != 0:
        sys.exit("Docker is not available to this user. See DOCKER_SETUP.md.")
    ok = True

    def check(cond, msg, detail=""):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + msg + (f"   [{detail}]" if (detail and not cond) else ""))
        ok &= bool(cond)

    good = run_case("good", GOOD)
    check(good["status"] == "ok" and good.get("pnl", 0) > 0, "good trader runs in the container and scores a profit",
          f"{good['status']} {good['message'][:100]} {good['_stderr'][-300:]}")
    again = run_case("good2", GOOD)
    check(again.get("pnl") == good.get("pnl"), "same code in the container twice gives identical PnL")

    loop = run_case("loop", "class Trader:\n    def run(self, s):\n        while True: pass\n")
    check(loop["status"] == "error" and "time-limit" in loop["message"] and loop["_wall"] < 90,
          f"infinite loop is killed and the run aborts ({loop['_wall']}s)", f"{loop['status']} {loop['message'][:100]}")

    mem = run_case("mem", "class Trader:\n    def __init__(self): self.h = []\n    def run(self, s):\n        for _ in range(100): self.h.append(b'x' * 10**8)\n        return {}, 0, ''\n")
    check(mem["status"] in ("ok", "error") and mem["_wall"] < 120 and mem["status"] != "NO_RESULT",
          f"memory bomb is contained and the job still reports ({mem['_wall']}s)", f"{mem['status']} {mem['_stderr'][-200:]}")

    pandas_ok = run_case("pandas", "import pandas as pd, numpy as np\nclass Trader:\n    def run(self, s): return {}, 0, ''\n")
    check(pandas_ok["status"] == "ok", "pandas and numpy import inside the container", pandas_ok["message"][:200])

    probe = run_case("probe", PROBE)
    logs = probe.get("logs", "")
    check("UID 65534" in logs, "participant process runs as uid 65534", logs[:200])
    for label in ("network", "read_engine_env", "read_scenario_src", "read_bots_src", "write_app",
                  "write_tmp", "kill_engine", "setuid_root"):
        check(f"BLOCKED {label}" in logs and f"DID {label}" not in logs, f"after allowlist bypass, '{label}' is blocked", logs[:400])

    check(not leftover_containers(), "no test containers left running or stopped (--rm works)")

    if "--timing" in sys.argv:
        print("\nTiming under the container's CPU cap (--cpus 1):")
        for n in ("json_state", "pandas_moderate", "pandas_heavy", "pandas_extreme"):
            code = open(os.path.join(HERE, "timing_stress", n + ".py")).read()
            r = run_case("t" + n.replace("_", ""), code)
            print(f"  {n:16s} {r['status']:6s} mean {r.get('mean_tick_ms', float('nan')):6.2f} ms  "
                  f"p99 {r.get('p99_tick_ms', float('nan')):6.2f}  max {r.get('max_tick_ms', float('nan')):6.2f}  "
                  f"timeouts {r.get('timeouts', '?')}  wall {r['_wall']}s")

    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED: see DOCKER_SETUP.md, Troubleshooting")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
