from ranking import rank, rank_key

R = lambda pnl, dd=0.0, ab=False: {"pnl": pnl, "max_drawdown": dd, "aborted": ab}
NS = {"status": "none", "pnl": 0.0, "max_drawdown": 0.0, "no_submission": True, "aborted": False}
res = {"pos_big": R(500), "dup1": R(77, 1.0), "dup2": R(77, 1.0), "pos_small": R(10, 0.0),
       "tie_a": R(10, 3.0), "tie_b": R(10, 9.0), "zero_done": R(0.0),
       "aborted1": R(0.0, ab=True), "aborted2": R(0.0, ab=True),
       "nosub1": dict(NS), "nosub2": dict(NS),
       "neg_small": R(-5), "neg_big": R(-400)}
out = [(r, t) for r, t, _ in rank(res)]
expected = [(1, "pos_big"), (2, "dup1"), (2, "dup2"), (4, "pos_small"), (5, "tie_a"), (6, "tie_b"),
            (7, "zero_done"), (8, "aborted1"), (8, "aborted2"), (10, "nosub1"), (10, "nosub2"),
            (12, "neg_small"), (13, "neg_big")]
assert out == expected, out
try:
    rank_key({"internal": True})
    raise SystemExit("internal result must not be rankable")
except ValueError:
    pass
print("ranking OK: PnL>=0 > aborted (broken last submission) > no submission > negative; "
      "ties on PnL broken by drawdown; equal teams share a rank")
