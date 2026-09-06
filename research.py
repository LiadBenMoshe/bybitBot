"""Walk-forward research harness.

Scores a strategy over several independent time windows instead of one, and
reports mean / standard deviation / t-statistic so a result can be judged rather
than eyeballed. A single-window backtest will happily show an edge that four
windows dissolve; this exists to catch that before it reaches live trading.

Any object implementing the Backtester's duck-typed interface can be evaluated:

    apply_indicators(frame) -> frame
    generate_signal(symbol, frame, indicators_ready=False) -> Signal

so alternative signals can be compared against the shipped strategy under the
identical cost model, sizing, exits and gate. Only the entry rule varies.

Usage:
    python research.py                 # shipped strategy vs selectivity ladder
    python research.py 15 3000 4       # timeframe, candles, windows
"""
from __future__ import annotations

import statistics
import sys
from typing import Callable, Optional

import pandas as pd

from backtest import Backtester, BacktestResult
from config import Settings, get_settings
from kline_cache import load_klines
from strategy import build_strategy


class WalkForward:
    """Fetches history once, then replays it across consecutive windows."""

    def __init__(self, symbols: list[str], timeframe: str, total_candles: int, segments: int) -> None:
        self.symbols = symbols
        self.timeframe = timeframe
        self.total = total_candles
        self.segments = segments
        self.window = total_candles // segments
        self.raw: dict[str, pd.DataFrame] = {}

    def fetch(self, use_cache: bool = True, refresh: bool = False) -> None:
        """Load history for every symbol and align them on shared timestamps.

        Windows are positional, so symbols with a shorter listing (or missing
        candles) would otherwise be scored over different dates than the rest.
        Every symbol is trimmed to the timestamps they all share, and the window
        length is recomputed from that common length.
        """
        settings = get_settings()
        client = Backtester(settings).client
        for symbol in self.symbols:
            if use_cache:
                self.raw[symbol] = load_klines(
                    client, settings.category, symbol, self.timeframe, self.total, refresh=refresh
                )
            else:
                self.raw[symbol] = client.get_kline_history(settings.category, symbol, self.timeframe, self.total)
        common = None
        for frame in self.raw.values():
            stamps = set(frame["timestamp"])
            common = stamps if common is None else common & stamps
        assert common, "No shared timestamps across symbols"
        for symbol, frame in self.raw.items():
            aligned = frame[frame["timestamp"].isin(common)].sort_values("timestamp").reset_index(drop=True)
            self.raw[symbol] = aligned
        lengths = {len(frame) for frame in self.raw.values()}
        assert len(lengths) == 1, f"Symbols still misaligned after trimming: {lengths}"
        self.total = lengths.pop()
        self.window = self.total // self.segments
        sample = self.raw[self.symbols[0]]
        print(
            f"  {len(sample)} shared candles  {sample['timestamp'].iloc[0]:%Y-%m-%d}"
            f" -> {sample['timestamp'].iloc[-1]:%Y-%m-%d}"
        )

    def _slice(self, symbol: str, segment: int) -> pd.DataFrame:
        start = segment * self.window
        return self.raw[symbol].iloc[start:start + self.window].reset_index(drop=True)

    def build_settings(self, overrides: Optional[dict] = None) -> Settings:
        """Fresh Settings with overrides applied; validation problems are printed, not raised.

        Research may deliberately explore values outside the live limits, but a
        candidate that violates a cross-field rule (target below stop, break-even
        inside the cost band) is usually a mistake, so say so loudly.
        """
        settings = get_settings()
        settings.timeframe = self.timeframe
        for key, value in (overrides or {}).items():
            if not hasattr(settings, key):
                raise AttributeError(f"Unknown settings override: {key}")
            setattr(settings, key, value)
        try:
            settings.validate()
        except ValueError as exc:
            print(f"  [validate] {exc}")
        return settings

    def evaluate_detailed(
        self,
        overrides: Optional[dict] = None,
        strategy_factory: Optional[Callable[[Settings], object]] = None,
        segments: Optional[list[int]] = None,
        symbols: Optional[list[str]] = None,
        collect_trades: bool = False,
    ) -> list[list[BacktestResult]]:
        """Replay every window and return the full result per window and symbol."""
        settings = self.build_settings(overrides)
        basket = symbols or self.symbols
        results: list[list[BacktestResult]] = []
        for segment in segments if segments is not None else range(self.segments):
            bt = Backtester(settings)
            bt.collect_trades = collect_trades
            if strategy_factory is not None:
                bt.strategy = strategy_factory(settings)
            bt.client.get_kline_history = (
                lambda category, symbol, interval, total, _s=segment: self._slice(symbol, _s)
            )
            results.append([bt.run(symbol, candles=self.window) for symbol in basket])
        return results

    def evaluate(
        self,
        overrides: Optional[dict] = None,
        strategy_factory: Optional[Callable[[Settings], object]] = None,
    ) -> tuple[list[float], int]:
        """Return (net % per symbol for each window, total trades)."""
        nets: list[float] = []
        trades = 0
        for window in self.evaluate_detailed(overrides, strategy_factory):
            nets.append(sum(r.total_return_pct for r in window) / len(window))
            trades += sum(r.trades for r in window)
        return nets, trades

    def buy_and_hold(self) -> tuple[list[float], int]:
        """Benchmark: a strategy that cannot beat holding is not worth its execution risk."""
        nets = []
        for segment in range(self.segments):
            total = 0.0
            for symbol in self.symbols:
                frame = self._slice(symbol, segment)
                first = float(frame["close"].iloc[0])
                last = float(frame["close"].iloc[-1])
                total += (last - first) / first * 100
            nets.append(total / len(self.symbols))
        return nets, 0


def summarize(label: str, nets: list[float], trades: int) -> None:
    mean = statistics.mean(nets)
    sd = statistics.stdev(nets) if len(nets) > 1 else 0.0
    # t against zero; |t| below ~2 means the result is indistinguishable from noise.
    t_stat = mean / (sd / len(nets) ** 0.5) if sd > 0 else 0.0
    up = sum(1 for n in nets if n > 0)
    print(f"{label:<30}{mean:>+8.2f}{sd:>8.2f}{t_stat:>8.2f}   {up}/{len(nets)}  {trades:>6}")


def main() -> None:
    timeframe = sys.argv[1] if len(sys.argv) > 1 else "60"
    total = int(sys.argv[2]) if len(sys.argv) > 2 else 6000
    segments = int(sys.argv[3]) if len(sys.argv) > 3 else 8

    symbols = get_settings().symbols
    harness = WalkForward(symbols, timeframe, total, segments)
    print(f"Fetching {total} x {timeframe}m candles for {len(symbols)} symbols")
    harness.fetch()
    print(f"\n{segments} windows of ~{harness.window * int(timeframe) / 60 / 24:.0f} days each")
    print(f"\n{'CONFIG':<30}{'MEAN%':>8}{'SD':>8}{'t':>8}   UP    TRADES")
    print("-" * 72)

    summarize("buy & hold", *harness.buy_and_hold())
    summarize("shipped strategy", *harness.evaluate(strategy_factory=build_strategy))
    for ratio, reward_risk in ((4.0, 1.5), (8.0, 1.8), (12.0, 2.0), (16.0, 2.2)):
        summarize(
            f"inverted ratio={ratio:g} rr={reward_risk}",
            *harness.evaluate(
                {
                    "invert_signals": True,
                    "min_target_to_cost_ratio": ratio,
                    "min_net_reward_risk": reward_risk,
                },
                strategy_factory=build_strategy,
            ),
        )
    print("\n|t| < 2 means the result cannot be distinguished from zero.")


if __name__ == "__main__":
    main()
