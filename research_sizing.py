"""Sizing and leverage study on a candidate's simulated trade list.

    python research_sizing.py --trades data/research/round1_trades.csv --candidate B2_htf_trend_taker
    python research_sizing.py --trades ... --candidate NAME --multipliers 0.5,1,2,3 --paths 2000

Each simulated trade carries its return as a fraction of equity at the sizing
the backtest used. Scaling that return by a multiplier m is what "enter with
more money" means: m = 2 doubles the notional of every trade. The script finds
the Kelly-optimal multiplier by maximising expected log growth, block-
bootstraps the trade sequence by window to estimate drawdown and ruin
probabilities at each multiplier, and prints the sizing rule from the plan.

Leverage itself never changes a simulated return; it only sets how much margin
the exchange demands and therefore where liquidation sits. The recommendation
therefore reports notional first and derives the leverage that funds it with
the liquidation distance still well beyond the stop.
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

from config import get_settings


def load_trades(path: Path, candidate: str, scope: str) -> dict[int, list[dict]]:
    by_window: dict[int, list[dict]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["candidate"] != candidate or row["scope"] != scope:
                continue
            by_window[int(row["window"])].append(
                {
                    "exit_ts": row["exit_ts"],
                    "ret": float(row["return_pct"]) / 100.0,
                    "ratio": float(row["notional_ratio"]),
                    "stop_pct": float(row["stop_distance_pct"]) / 100.0,
                }
            )
    for trades in by_window.values():
        trades.sort(key=lambda t: t["exit_ts"])
    return by_window


def equity_path(returns: list[float], multiplier: float) -> tuple[float, float]:
    """Compound scaled returns; return (terminal multiple, max drawdown fraction)."""
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in returns:
        equity *= 1 + max(r * multiplier, -0.99)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
        if equity <= 0.01:
            break
    return equity, max_dd


def kelly_multiplier(returns: list[float], upper: float = 6.0) -> float:
    """Multiplier maximising mean log growth; 0 when no positive-growth multiplier exists."""
    best_m, best_g = 0.0, 0.0
    m = 0.05
    while m <= upper:
        growth = statistics.mean(math.log(1 + max(r * m, -0.99)) for r in returns)
        if growth > best_g:
            best_m, best_g = m, growth
        m += 0.05
    return round(best_m, 2)


def bootstrap(by_window: dict[int, list[dict]], multiplier: float, paths: int, rng: random.Random) -> dict:
    windows = sorted(by_window)
    terminals: list[float] = []
    drawdowns: list[float] = []
    for _ in range(paths):
        sampled = [by_window[rng.choice(windows)] for _ in windows]
        returns = [t["ret"] for block in sampled for t in block]
        terminal, max_dd = equity_path(returns, multiplier)
        terminals.append(terminal)
        drawdowns.append(max_dd)
    terminals.sort()
    drawdowns.sort()

    def pct(values: list[float], q: float) -> float:
        return values[min(len(values) - 1, int(q * len(values)))]

    return {
        "multiplier": multiplier,
        "median_terminal_pct": (pct(terminals, 0.5) - 1) * 100,
        "p05_terminal_pct": (pct(terminals, 0.05) - 1) * 100,
        "median_dd_pct": pct(drawdowns, 0.5) * 100,
        "p95_dd_pct": pct(drawdowns, 0.95) * 100,
        "p_dd_over_25": sum(1 for d in drawdowns if d > 0.25) / paths,
        "p_half_equity": sum(1 for t in terminals if t < 0.5) / paths,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trades", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--scope", default="insample")
    parser.add_argument("--multipliers", default="0.5,1,2,3")
    parser.add_argument("--paths", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--t", type=float, default=None, help="in-sample t of the candidate (from the sweep CSV)")
    parser.add_argument("--holdout-positive", action="store_true", help="both hold-out windows were positive")
    args = parser.parse_args()

    by_window = load_trades(Path(args.trades), args.candidate, args.scope)
    if not by_window:
        raise SystemExit(f"No {args.scope} trades for {args.candidate!r} in {args.trades}")
    trades = [t for block in by_window.values() for t in block]
    returns = [t["ret"] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [-r for r in returns if r <= 0]
    settings = get_settings()

    print(f"{args.candidate}: {len(trades)} trades over {len(by_window)} windows")
    print(f"  win rate {len(wins) / len(returns):.1%}   avg win {statistics.mean(wins) * 100 if wins else 0:.3f}%   "
          f"avg loss {statistics.mean(losses) * 100 if losses else 0:.3f}%   mean {statistics.mean(returns) * 100:+.4f}%/trade")
    print(f"  median notional ratio {statistics.median(t['ratio'] for t in trades):.2f}x equity   "
          f"median stop distance {statistics.median(t['stop_pct'] for t in trades) * 100:.2f}%")
    k = kelly_multiplier(returns)
    print(f"  Kelly-optimal multiplier {k:.2f}x  ->  quarter-Kelly {k / 4:.2f}x current sizing")

    rng = random.Random(args.seed)
    print(f"\n{'MULT':>5}{'MED TERM%':>11}{'P05 TERM%':>11}{'MED DD%':>9}{'P95 DD%':>9}{'P(DD>25%)':>11}{'P(<50%)':>9}")
    for m in (float(x) for x in args.multipliers.split(",")):
        b = bootstrap(by_window, m, args.paths, rng)
        print(f"{m:>5.2f}{b['median_terminal_pct']:>+11.2f}{b['p05_terminal_pct']:>+11.2f}{b['median_dd_pct']:>9.2f}"
              f"{b['p95_dd_pct']:>9.2f}{b['p_dd_over_25']:>11.2f}{b['p_half_equity']:>9.2f}")

    print("\nRecommendation (plan rule):")
    validated = args.t is not None and args.t >= 2.4 and args.holdout_positive
    if not validated:
        print("  Edge NOT validated (needs t >= 2.4 in-sample and both hold-out windows positive).")
        print("  Keep MAX_POSITION_NOTIONAL_PCT <= 1.0 and DEFAULT_LEVERAGE <= 3. Leverage on an unproven edge only")
        print("  changes how fast the account reaches the drawdowns in the table above.")
    else:
        median_ratio = statistics.median(t["ratio"] for t in trades)
        r = min(k / 4 * median_ratio, 2.0)
        leverage = min(10, math.ceil(1.5 * r * settings.max_positions / 0.8))
        print(f"  MAX_POSITION_NOTIONAL_PCT = {r:.2f}  (quarter-Kelly x median ratio, capped at 2.0)")
        print(f"  DEFAULT_LEVERAGE = {leverage}  (funds {settings.max_positions} concurrent positions with margin to spare)")
        print("  Paper-trade first; go live at half this size.")


if __name__ == "__main__":
    main()
