"""Walk-forward sweep over the pre-registered candidate list.

    python research_sweep.py --tf 60 --candles 7500 --windows 10 --insample 8 --tag round1
    python research_sweep.py --tag round1 --holdout B2_htf_trend_taker
    python research_sweep.py --tag round2 --only A3_A2_trail2.0_time24 --regime 0.3,0.9 --drop SOLUSDT

Writes data/research/<tag>.csv (one row per candidate) and
data/research/<tag>_trades.csv (every simulated trade). The in-sample windows
are 0..insample-1; the remaining windows are the hold-out, which may be spent
on exactly one candidate per tag.
"""
from __future__ import annotations

import argparse
import csv
import statistics
from dataclasses import fields
from pathlib import Path
from typing import Any, Optional

from backtest import BacktestResult
from config import get_settings
from research import WalkForward
from strategies_research import CANDIDATES, Candidate, ROUND2_BUILDERS, build_named_strategy

RESULTS_DIR = Path("data") / "research"

# Pre-registered acceptance bar. A candidate passes in-sample only if every
# rule holds; the hold-out is judged separately by --holdout.
ACCEPTANCE = {
    "min_t": 2.4,
    "min_up_share": 0.75,
    "min_pooled_pf": 1.35,
    "max_drawdown_pct": 25.0,
    "min_trades_per_window": 20,
}

SUMMARY_COLUMNS = [
    "name", "family", "scope", "windows", "mean_pct", "sd_pct", "t", "up", "trades",
    "pooled_pf", "max_dd_pct", "expectancy_r", "win_rate_pct", "assumed_p",
    "stop_share", "target_share", "flip_share", "time_share", "liquidations",
    "avg_bars_held", "funding_pct", "entries_missed", "peak_gross_exposure",
    "beats_bh", "pass", "fail_reasons", "per_window", "overrides",
]


def _t_stat(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    sd = statistics.stdev(values)
    return statistics.mean(values) / (sd / len(values) ** 0.5) if sd > 0 else 0.0


def _peak_gross_exposure(trades: list[dict[str, Any]]) -> float:
    """Largest sum of notional ratios open at the same instant across symbols."""
    events: list[tuple[Any, int, float]] = []
    for trade in trades:
        events.append((trade["entry_ts"], 1, trade["notional_ratio"]))
        events.append((trade["exit_ts"], 0, trade["notional_ratio"]))
    events.sort(key=lambda item: (item[0], item[1]))  # exits (0) before entries (1) at equal stamps
    peak = current = 0.0
    for _, kind, ratio in events:
        current += ratio if kind == 1 else -ratio
        peak = max(peak, current)
    return round(peak, 2)


def summarize(
    name: str,
    family: str,
    scope: str,
    windows: list[list[BacktestResult]],
    assumed_p: Optional[float],
    bh_mean: float,
    bh_sd: float,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    nets = [sum(r.total_return_pct for r in window) / len(window) for window in windows]
    flat = [r for window in windows for r in window]
    trades = sum(r.trades for r in flat)
    wins = sum(r.trades * r.win_rate_pct / 100 for r in flat)
    profit = sum(r.profit_sum for r in flat)
    loss = sum(r.loss_sum for r in flat)
    exits = {
        "stop": sum(r.stop_exits for r in flat),
        "target": sum(r.target_exits for r in flat),
        "flip": sum(r.flip_exits for r in flat),
        "time": sum(r.time_exits for r in flat),
    }
    bars = sum(r.avg_bars_held * r.trades for r in flat)
    mean = statistics.mean(nets)
    sd = statistics.stdev(nets) if len(nets) > 1 else 0.0
    t_stat = _t_stat(nets)
    up = sum(1 for n in nets if n > 0)
    pooled_pf = profit / loss if loss > 0 else (999.0 if profit > 0 else 0.0)
    max_dd = max((r.max_drawdown_pct for r in flat), default=0.0)
    all_trades = [
        {**t, "window": index} for index, window in enumerate(windows) for r in window for t in r.trade_list
    ]
    peak_exposure = max(
        (_peak_gross_exposure([t for r in window for t in r.trade_list]) for window in windows), default=0.0
    )
    beats_bh = mean > bh_mean and (sd <= bh_sd or mean > 0)

    reasons: list[str] = []
    if mean <= 0:
        reasons.append("mean<=0")
    if t_stat < ACCEPTANCE["min_t"]:
        reasons.append(f"t<{ACCEPTANCE['min_t']}")
    if up < ACCEPTANCE["min_up_share"] * len(nets):
        reasons.append("up<6/8")
    if pooled_pf < ACCEPTANCE["min_pooled_pf"]:
        reasons.append("pf<1.35")
    if max_dd > ACCEPTANCE["max_drawdown_pct"]:
        reasons.append("dd>25")
    if trades < ACCEPTANCE["min_trades_per_window"] * len(nets):
        reasons.append("trades<20/window")
    if not beats_bh:
        reasons.append("not>bh")

    return {
        "name": name,
        "family": family,
        "scope": scope,
        "windows": len(nets),
        "mean_pct": round(mean, 3),
        "sd_pct": round(sd, 3),
        "t": round(t_stat, 2),
        "up": f"{up}/{len(nets)}",
        "trades": trades,
        "pooled_pf": round(pooled_pf, 3),
        "max_dd_pct": round(max_dd, 2),
        "expectancy_r": round(statistics.mean([r.expectancy_r for r in flat if r.trades]), 4) if trades else 0.0,
        "win_rate_pct": round(wins / trades * 100, 1) if trades else 0.0,
        "assumed_p": assumed_p if assumed_p is not None else "",
        "stop_share": round(exits["stop"] / trades, 3) if trades else 0.0,
        "target_share": round(exits["target"] / trades, 3) if trades else 0.0,
        "flip_share": round(exits["flip"] / trades, 3) if trades else 0.0,
        "time_share": round(exits["time"] / trades, 3) if trades else 0.0,
        "liquidations": sum(r.liquidations for r in flat),
        "avg_bars_held": round(bars / trades, 2) if trades else 0.0,
        "funding_pct": round(sum(r.funding_paid_pct for r in flat), 3),
        "entries_missed": sum(r.entries_missed for r in flat),
        "peak_gross_exposure": peak_exposure,
        "beats_bh": beats_bh,
        "pass": not reasons,
        "fail_reasons": ";".join(reasons),
        "per_window": " ".join(f"{n:+.2f}" for n in nets),
        "overrides": repr(overrides),
        "_trades": all_trades,
    }


def buy_and_hold_row(harness: WalkForward, segments: list[int], symbols: list[str]) -> tuple[float, float, dict]:
    nets = []
    for segment in segments:
        total = 0.0
        for symbol in symbols:
            frame = harness._slice(symbol, segment)
            total += (float(frame["close"].iloc[-1]) - float(frame["close"].iloc[0])) / float(frame["close"].iloc[0]) * 100
        nets.append(total / len(symbols))
    mean = statistics.mean(nets)
    sd = statistics.stdev(nets) if len(nets) > 1 else 0.0
    row = {
        "name": "buy_and_hold", "family": "benchmark", "scope": "insample" if segments[0] == 0 else "holdout",
        "windows": len(nets), "mean_pct": round(mean, 3), "sd_pct": round(sd, 3), "t": round(_t_stat(nets), 2),
        "up": f"{sum(1 for n in nets if n > 0)}/{len(nets)}", "trades": 0,
        "per_window": " ".join(f"{n:+.2f}" for n in nets), "pass": False, "_trades": [],
    }
    return mean, sd, row


def write_rows(tag: str, rows: list[dict[str, Any]], append: bool) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = RESULTS_DIR / f"{tag}.csv"
    trades_path = RESULTS_DIR / f"{tag}_trades.csv"
    mode = "a" if append and summary_path.exists() else "w"
    with summary_path.open(mode, newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore")
        if mode == "w":
            writer.writeheader()
        for row in rows:
            writer.writerow(row)
    trade_columns = [
        "candidate", "scope", "window", "symbol", "side", "entry_ts", "exit_ts", "bars_held", "exit_reason",
        "entry_price", "exit_price", "gross_pct", "return_pct", "r_multiple", "notional_ratio",
        "stop_distance_pct", "funding_pct",
    ]
    mode = "a" if append and trades_path.exists() else "w"
    with trades_path.open(mode, newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=trade_columns, extrasaction="ignore")
        if mode == "w":
            writer.writeheader()
        for row in rows:
            for trade in row.get("_trades", []):
                writer.writerow({"candidate": row["name"], "scope": row["scope"], **trade})


def existing_holdouts(tag: str) -> set[str]:
    path = RESULTS_DIR / f"{tag}.csv"
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["name"] for row in csv.DictReader(handle) if row.get("scope") == "holdout"}


def print_table(rows: list[dict[str, Any]]) -> None:
    header = f"{'CANDIDATE':<28}{'MEAN%':>8}{'SD':>7}{'t':>7}  {'UP':>5}{'TRADES':>8}{'PF':>7}{'DD%':>7}{'WR%':>6}{'STOP':>6}{'BARS':>6}  PASS"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['name']:<28}{row['mean_pct']:>+8.2f}{row.get('sd_pct', 0):>7.2f}{row['t']:>7.2f}  {row['up']:>5}"
            f"{row['trades']:>8}{row.get('pooled_pf', 0):>7.2f}{row.get('max_dd_pct', 0):>7.2f}"
            f"{row.get('win_rate_pct', 0):>6.1f}{row.get('stop_share', 0):>6.2f}{row.get('avg_bars_held', 0):>6.1f}"
            f"  {'PASS' if row.get('pass') else (row.get('fail_reasons', '') or '-')}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tf", default="60")
    parser.add_argument("--candles", type=int, default=7500)
    parser.add_argument("--windows", type=int, default=10)
    parser.add_argument("--insample", type=int, default=8)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--refresh", action="store_true", help="extend the kline cache before running")
    parser.add_argument("--only", default="", help="comma-separated candidate names to run")
    parser.add_argument("--holdout", default="", help="evaluate ONE candidate on the hold-out windows")
    parser.add_argument("--regime", default="", help="round 2: ATR-percentile band low,high applied to --only")
    parser.add_argument("--sessions", default="", help="round 2: comma-separated UTC hours to allow")
    parser.add_argument("--drop", default="", help="round 2: comma-separated symbols to exclude")
    parser.add_argument("--symbols", default="", help="override the basket (default TRADING_SYMBOLS)")
    args = parser.parse_args()

    settings = get_settings()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or list(settings.symbols)
    harness = WalkForward(symbols, args.tf, args.candles, args.windows)
    print(f"Loading {args.candles} x {args.tf}m candles for {len(symbols)} symbols (cache: data/klines)")
    harness.fetch(use_cache=True, refresh=args.refresh)
    insample = list(range(args.insample))
    holdout = list(range(args.insample, args.windows))
    print(f"{args.windows} windows of ~{harness.window * int(args.tf) / 60 / 24:.0f} days; in-sample {insample}, hold-out {holdout}")

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    candidates = [c for c in CANDIDATES if not only or c.name in only]
    if only and not candidates:
        raise SystemExit(f"No candidate matches {sorted(only)}")

    # Round-2 modifiers wrap the selected candidates rather than adding new rows.
    if args.regime or args.sessions or args.drop:
        low, high = (float(x) for x in args.regime.split(",")) if args.regime else (0.0, 1.0)
        sessions = {int(h) for h in args.sessions.split(",") if h.strip()} or None
        dropped = {s.strip().upper() for s in args.drop.split(",") if s.strip()}
        wrapped: list[Candidate] = []
        for cand in candidates:
            base_name = "indicator"
            if cand.strategy_factory is not None:
                base_name = cand.strategy_factory(settings).__class__.__name__
                base_name = {"HTFTrendBreakout": "htf_trend", "MeanReversion60": "mean_reversion"}[base_name]
            factory = cand.strategy_factory
            suffix = ""
            if args.regime or args.sessions:
                factory = ROUND2_BUILDERS["regime"](base_name, low=low, high=high, sessions=sessions)
                suffix += f"_regime{low:g}-{high:g}" + (f"_sess{len(sessions)}" if sessions else "")
            basket = [s for s in (cand.symbols or symbols) if s not in dropped] or None
            if dropped:
                suffix += "_drop" + "+".join(sorted(dropped))
            wrapped.append(Candidate(cand.name + suffix, cand.family + "+D/E", dict(cand.overrides), factory, basket))
        candidates = wrapped

    if args.holdout:
        spent = existing_holdouts(args.tag)
        if spent and args.holdout not in spent:
            raise SystemExit(f"Hold-out for tag {args.tag!r} already spent on {sorted(spent)}; refusing a second name.")
        candidates = [c for c in candidates if c.name == args.holdout]
        if not candidates:
            raise SystemExit(f"--holdout name {args.holdout!r} not found among candidates")
        segments, scope = holdout, "holdout"
    else:
        segments, scope = insample, "insample"

    rows: list[dict[str, Any]] = []
    bh_mean, bh_sd, bh_row = buy_and_hold_row(harness, segments, symbols)
    rows.append(bh_row)
    for cand in candidates:
        basket = cand.symbols or symbols
        results = harness.evaluate_detailed(
            cand.overrides, cand.strategy_factory, segments=segments, symbols=basket, collect_trades=True
        )
        assumed = None
        if cand.strategy_factory is not None:
            assumed = getattr(cand.strategy_factory(settings), "assumed_p", None)
        row = summarize(cand.name, cand.family, scope, results, assumed, bh_mean, bh_sd, cand.overrides)
        rows.append(row)
        print_table([row])
    print()
    print_table(rows)
    write_rows(args.tag, rows, append=bool(args.holdout) or bool(args.regime or args.sessions or args.drop))
    print(f"\nwrote {RESULTS_DIR / (args.tag + '.csv')}")
    print(f"Acceptance: mean>0, t>={ACCEPTANCE['min_t']}, up>=6/8, pooled PF>={ACCEPTANCE['min_pooled_pf']}, "
          f"DD<={ACCEPTANCE['max_drawdown_pct']}%, >={ACCEPTANCE['min_trades_per_window']} trades/window, beats buy&hold.")


if __name__ == "__main__":
    main()
