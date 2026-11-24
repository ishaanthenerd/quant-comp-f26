"""
ranking.py: the final-ranking rule.

Tiers, best to worst:
  1. completed runs with PnL >= 0, by PnL (higher first), ties by lower max drawdown
  2. aborted / failed-to-load runs (scored PnL 0, never disqualified). A team's LAST submission counts even if it is broken.
  3. teams with NO submission (scored PnL 0)
  4. completed runs with negative PnL, by PnL (less negative first), ties by lower drawdown

Results flagged `internal` (our grading failure) are NOT rankable: they must be
re-run first, so rank() raises rather than guessing.
"""


def rank_key(result):
    if result.get("internal"):
        raise ValueError("internal grading error: re-run this submission before ranking")
    if result.get("no_submission"):
        return (2, 0.0, 0.0)
    if result.get("aborted"):
        return (1, 0.0, 0.0)
    pnl = result["pnl"]
    return (0 if pnl >= 0 else 3, -pnl, result.get("max_drawdown", 0.0))


def rank(results):
    """results: {team: result_dict} -> [(rank, team, result)], tied teams share a rank."""
    keyed = sorted(((rank_key(r), team, r) for team, r in results.items()), key=lambda x: (x[0], x[1]))
    out, prev_key, prev_rank = [], None, 0
    for i, (k, team, r) in enumerate(keyed, start=1):
        if k != prev_key:
            prev_rank, prev_key = i, k
        out.append((prev_rank, team, r))
    return out
