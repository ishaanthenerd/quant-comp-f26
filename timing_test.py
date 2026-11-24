"""Run the stress traders through the real sandbox (as root, uid-dropped) and report per-tick latency vs the 100 ms limit."""
import json, os, sys, glob
os.environ.setdefault("TEST_AS_MB", "4096")
import test_sandbox as T

T.stage()
print(f"{'trader':18s} {'kind':6s} {'status':7s} {'mean ms':>8s} {'p99 ms':>8s} {'max ms':>8s} {'timeouts':>8s} {'errors':>6s} {'wall s':>7s}")
for path in sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "timing_stress", "*.py"))):
    name = os.path.basename(path)[:-3]
    code = open(path).read()
    for kind in sys.argv[1:] or ["slice"]:
        r = T.run(f"{name}_{kind}", code, kind)
        print(f"{name:18s} {kind:6s} {r['status']:7s} {r.get('mean_tick_ms', float('nan')):8.2f} {r.get('p99_tick_ms', float('nan')):8.2f} "
              f"{r.get('max_tick_ms', float('nan')):8.2f} {r['timeouts']:8d} {r['errors']:6d} {r['_wall']:7.1f}  {r['message'][:60]}")
        if r["errors"] and r["logs"]:
            print("   ", r["logs"][:300].replace("\n", " | "))
