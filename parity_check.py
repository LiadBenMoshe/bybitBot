"""Backtester vs. paper-trader parity check.

    python parity_check.py --log-dir <scratch dir>

Feeds one synthetic hourly series through ``Backtester.run`` and through the
``TraderEngine`` per-bar sequence (the same calls ``_run_loop`` makes), using a
scripted strategy that emits buy/sell at fixed timestamps with ATR exits. The
two engines must then produce the same trades: same entry and exit prices,
same exit reasons, same bars held. Any difference is a simulator/live gap and
the backtest cannot be trusted for that exit type until it is closed.

Scenarios cover fixed targets, trailing stops, maker and taker entries, the
time stop and a signal flip. ``COOLDOWN_BARS`` is forced to 0 because the
backtester deliberately does not re-enter on the bar of a flip exit while the
trader does; that gap is known and documented in CLAUDE.md.
"""
from __future__ import annotations

import argparse
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from api import KlineEvent
from backtest import Backtester
from config import get_settings
from models import Signal
from trader import TraderEngine

SYMBOL = "TESTUSDT"


class ScriptedStrategy:
    """Emits a fixed action at scripted timestamps; ATR exits at 1.0 / 2.4 multiples."""

    def __init__(self, script: dict[pd.Timestamp, str], stop_mult: float = 1.0, target_mult: float = 2.4) -> None:
        self.script = script
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def min_atr_pct(self) -> float:
        return 0.0

    def apply_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        df = frame.copy()
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
        ).max(axis=1)
        df["atr"] = tr.rolling(14).mean()
        df["atr_pct"] = df["atr"] / df["close"]
        return df

    def signal_from_rows(self, symbol: str, latest: Any, previous: Any) -> Signal:
        stamp = pd.Timestamp(latest["timestamp"])
        action = self.script.get(stamp, "hold")
        close = float(latest["close"])
        atr = float(latest["atr"]) if not pd.isna(latest["atr"]) else 0.0
        stop = target = None
        if action == "buy":
            stop, target = close - atr * self.stop_mult, close + atr * self.target_mult
        elif action == "sell":
            stop, target = close + atr * self.stop_mult, close - atr * self.target_mult
        return Signal(symbol, action, close, stamp.to_pydatetime(), 0.7 if action != "hold" else 0.0,
                      f"scripted {action}", stop, target)

    def generate_signal(self, symbol: str, frame: pd.DataFrame, indicators_ready: bool = False) -> Signal:
        df = frame if indicators_ready else self.apply_indicators(frame)
        return self.signal_from_rows(symbol, df.iloc[-1], df.iloc[-2] if len(df) > 1 else df.iloc[-1])


def synthetic_frame(bars: int = 320, seed: int = 3) -> pd.DataFrame:
    rng = random.Random(seed)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for i in range(bars):
        drift = 0.004 * math.sin(i / 9.0)  # slow waves so stops and targets both get hit
        price *= 1 + drift + rng.gauss(0, 0.004)
        high = price * (1 + abs(rng.gauss(0, 0.005)))
        low = price * (1 - abs(rng.gauss(0, 0.005)))
        open_ = price * (1 + rng.gauss(0, 0.002))
        rows.append({
            "timestamp": pd.Timestamp(start + timedelta(hours=i)),
            "open": open_, "high": max(high, open_, price), "low": min(low, open_, price), "close": price,
            "volume": 1000.0, "turnover": 1000.0 * price,
        })
    return pd.DataFrame(rows)


def run_backtest(settings, frame: pd.DataFrame, strategy: ScriptedStrategy) -> list[dict[str, Any]]:
    bt = Backtester(settings)
    bt.strategy = strategy
    bt.collect_trades = True
    bt.client.get_kline_history = lambda category, symbol, interval, total: frame.copy()
    result = bt.run(SYMBOL, len(frame))
    return [
        {"entry": round(t["entry_price"], 6), "exit": round(t["exit_price"], 6), "reason": t["exit_reason"],
         "bars": t["bars_held"], "side": t["side"]}
        for t in result.trade_list
    ]


def run_trader(settings, frame: pd.DataFrame, strategy: ScriptedStrategy) -> list[dict[str, Any]]:
    engine = TraderEngine(settings)
    engine.strategy = strategy
    closed: list[dict[str, Any]] = []
    original_close = engine._close_position

    def recording_close(symbol: str, price: float, reason: str) -> None:
        position = engine.positions.get(symbol)
        if position:
            closed.append({"entry": round(position.entry_price, 6), "exit": round(price, 6),
                           "reason": reason.split(":")[0].strip(), "bars": position.bars_held, "side": position.side})
        original_close(symbol, price, reason)

    engine._close_position = recording_close  # type: ignore[method-assign]
    for row in frame.itertuples(index=False):
        event = KlineEvent(SYMBOL, row.timestamp.to_pydatetime(), row.open, row.high, row.low, row.close, row.volume, True)
        engine._update_history(SYMBOL, event)
        engine._mark_to_market(SYMBOL, event.close)
        engine._evaluate_risk_exits(SYMBOL, event)
        engine._reconcile_pending_entry(SYMBOL, event)
        history = engine.market_history[SYMBOL]
        if len(history) < 50:
            continue
        engine._process_signal(engine.strategy.generate_signal(SYMBOL, history))
    return closed


def compare(label: str, bt_trades: list[dict], tr_trades: list[dict]) -> int:
    mismatches = 0
    print(f"\n== {label}: backtest {len(bt_trades)} trades, trader {len(tr_trades)} trades")
    for i in range(max(len(bt_trades), len(tr_trades))):
        a = bt_trades[i] if i < len(bt_trades) else None
        b = tr_trades[i] if i < len(tr_trades) else None
        same = a is not None and b is not None and all(
            (abs(a[k] - b[k]) < 1e-6 if isinstance(a[k], float) else a[k] == b[k]) for k in ("entry", "exit", "reason", "bars", "side")
        )
        mismatches += 0 if same else 1
        flag = "  " if same else "!!"
        print(f"{flag} #{i + 1:<3} backtest {a}   trader {b}")
    return mismatches


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log-dir", required=True, help="scratch directory for the trader's trade log")
    args = parser.parse_args()
    frame = synthetic_frame()
    stamps = list(frame["timestamp"])
    script = {stamps[70]: "buy", stamps[95]: "sell", stamps[120]: "buy", stamps[123]: "sell",  # 123 flips 120 if still open
              stamps[160]: "sell", stamps[200]: "buy", stamps[240]: "sell", stamps[280]: "buy"}

    scenarios = [
        ("fixed target, maker entry", {"take_profit_mode": "fixed", "maker_entry_enabled": True, "max_bars_held": 0}),
        ("fixed target, taker entry", {"take_profit_mode": "fixed", "maker_entry_enabled": False, "max_bars_held": 0}),
        ("fixed target, taker, time stop 6", {"take_profit_mode": "fixed", "maker_entry_enabled": False, "max_bars_held": 6}),
        ("trail 1.5, taker entry", {"take_profit_mode": "trail", "maker_entry_enabled": False, "max_bars_held": 0}),
        ("trail 1.5, maker, time stop 12", {"take_profit_mode": "trail", "maker_entry_enabled": True, "max_bars_held": 12}),
    ]
    total = 0
    for label, overrides in scenarios:
        settings = get_settings()
        settings.paper_trading = True
        settings.telegram_bot_token = ""
        settings.log_dir = Path(args.log_dir)
        settings.symbols = [SYMBOL]
        settings.cooldown_bars = 0
        settings.max_consecutive_losses = 0
        settings.funding_rate_per_8h = 0.0
        settings.filter_symbols_by_backtest = False
        for key, value in overrides.items():
            setattr(settings, key, value)
        strategy = ScriptedStrategy(script)
        total += compare(label, run_backtest(settings, frame, strategy), run_trader(settings, frame, strategy))
    print(f"\nTOTAL MISMATCHES: {total}")
    raise SystemExit(1 if total else 0)


if __name__ == "__main__":
    main()
