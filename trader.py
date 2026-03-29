from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd

from api import BybitClient, MarketDataStream
from config import Settings
from logger import append_jsonl
from models import Position, Signal, TradeRecord
from notifier import TelegramNotifier, build_trade_message
from strategy import IndicatorStrategy, StrategyConfig
from backtest import Backtester, BacktestResult


class TraderEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = BybitClient(settings.api_key, settings.api_secret, testnet=settings.testnet)
        self.strategy = IndicatorStrategy(
            StrategyConfig(
                ema_fast=settings.ema_fast,
                ema_slow=settings.ema_slow,
                trend_ema=settings.trend_ema,
                rsi_period=settings.rsi_period,
                rsi_long_threshold=settings.rsi_long_threshold,
                rsi_short_threshold=settings.rsi_short_threshold,
                macd_fast=settings.macd_fast,
                macd_slow=settings.macd_slow,
                macd_signal=settings.macd_signal,
                atr_period=settings.atr_period,
                atr_min_pct=settings.atr_min_pct,
                signal_score_threshold=settings.signal_score_threshold,
            )
        )
        self.market_data = MarketDataStream(
            self.client,
            category=settings.category,
            interval=settings.timeframe,
            poll_interval_seconds=settings.poll_interval_seconds,
        )
        self.notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        self.backtester = Backtester(settings)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.status = "Stopped"
        self.trade_count = 0
        self.realized_pnl = 0.0
        self.cash_balance = settings.initial_balance
        self.last_error = ""
        self.positions: dict[str, Position] = {}
        self.recent_trades: list[dict[str, Any]] = []
        self.market_history: dict[str, pd.DataFrame] = {}
        self.last_closed_at: dict[str, datetime] = {}
        self.active_symbols: list[str] = list(settings.symbols)
        self.symbol_filter_results: list[BacktestResult] = []

    def bootstrap(self) -> None:
        self.active_symbols = list(self.settings.symbols)
        if self.settings.filter_symbols_by_backtest:
            self.symbol_filter_results = self.backtester.run_for_symbols(self.settings.symbols)
            approved = [result.symbol for result in self.symbol_filter_results if result.passed_filter]
            rejected = [result.symbol for result in self.symbol_filter_results if not result.passed_filter]
            if approved:
                self.active_symbols = approved
                self.logger.info("Approved symbols after backtest filter: %s", approved)
            else:
                self.logger.warning("No symbols passed the backtest filter. Falling back to configured symbols.")
            if rejected:
                self.logger.info("Rejected symbols after backtest filter: %s", rejected)
        for symbol in self.active_symbols:
            frame = self.client.get_kline(self.settings.category, symbol, self.settings.timeframe, limit=250)
            if frame.empty:
                raise RuntimeError(f"No market data returned for {symbol}.")
            self.market_history[symbol] = frame
        if not self.settings.paper_trading:
            try:
                self.cash_balance = self.client.get_wallet_balance()
            except Exception as exc:
                self.logger.warning("Wallet balance fetch failed at startup: %s", exc)
        if not self.settings.paper_trading:
            for symbol in self.active_symbols:
                try:
                    self.client.set_leverage(self.settings.category, symbol, self.settings.leverage)
                except Exception as exc:
                    self.logger.warning("Leverage setup skipped for %s: %s", symbol, exc)

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        try:
            self.bootstrap()
            self.stop_event.clear()
            self.market_data.start(self.active_symbols)
            self.status = "Running"
            self.last_error = ""
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
            self.logger.info("Trader started in %s mode.", "paper" if self.settings.paper_trading else "live")
        except Exception as exc:
            self.status = "Stopped"
            self.last_error = str(exc)
            self.logger.exception("Failed to start trader: %s", exc)

    def stop(self) -> None:
        self.stop_event.set()
        self.market_data.stop()
        self.status = "Stopped"
        if self.thread:
            self.thread.join(timeout=5)

    def _run_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                event = self.market_data.get_event(timeout=1.0)
                if event is None:
                    self._refresh_live_positions()
                    continue
                if not event.confirm:
                    continue
                self._update_history(event.symbol, event)
                self._mark_to_market(event.symbol, event.close)
                self._evaluate_risk_exits(event.symbol, event.close)
                frame = self.market_history[event.symbol]
                if len(frame) < 50:
                    continue
                signal = self.strategy.generate_signal(event.symbol, frame)
                self._process_signal(signal)
        except Exception as exc:
            self.last_error = str(exc)
            self.status = "Stopped"
            self.logger.exception("Trader loop crashed: %s", exc)
            self.market_data.stop()

    def _update_history(self, symbol: str, event: Any) -> None:
        frame = self.market_history.get(symbol, pd.DataFrame())
        new_row = pd.DataFrame(
            [{
                "timestamp": pd.Timestamp(event.timestamp),
                "open": event.open,
                "high": event.high,
                "low": event.low,
                "close": event.close,
                "volume": event.volume,
                "turnover": event.close * event.volume,
            }]
        )
        frame = pd.concat([frame, new_row], ignore_index=True)
        frame = frame.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp").tail(500)
        self.market_history[symbol] = frame.reset_index(drop=True)

    def _calculate_position_size(self, price: float) -> float:
        risk_amount = self.cash_balance * self.settings.risk_per_trade
        stop_distance = max(price * self.settings.stop_loss_pct, 0.0001)
        return round(risk_amount / stop_distance, 6)

    def _process_signal(self, signal: Signal) -> None:
        with self.lock:
            existing = self.positions.get(signal.symbol)
            if signal.action == "hold":
                return
            action = self._effective_action(signal.action)
            if self._is_in_cooldown(signal.symbol, signal.timestamp):
                return
            if existing and ((existing.side == "long" and action == "buy") or (existing.side == "short" and action == "sell")):
                return
            if existing:
                self._close_position(signal.symbol, signal.price, f"Signal flip: {signal.reason}")
            if len(self.positions) >= self.settings.max_positions:
                return
            side = "long" if action == "buy" else "short"
            qty = self._calculate_position_size(signal.price)
            if qty <= 0:
                return
            self._open_position(signal.symbol, side, signal.price, qty, signal.reason)

    def _effective_action(self, action: str) -> str:
        if not self.settings.invert_signals:
            return action
        if action == "buy":
            return "sell"
        if action == "sell":
            return "buy"
        return action

    def _is_in_cooldown(self, symbol: str, timestamp: datetime) -> bool:
        if self.settings.cooldown_bars <= 0:
            return False
        last_closed = self.last_closed_at.get(symbol)
        if last_closed is None:
            return False
        try:
            minutes_per_bar = int(self.settings.timeframe)
        except ValueError:
            minutes_per_bar = 15
        return (timestamp - last_closed).total_seconds() < self.settings.cooldown_bars * minutes_per_bar * 60

    def _open_position(self, symbol: str, side: str, price: float, qty: float, reason: str) -> None:
        stop_loss = price * (1 - self.settings.stop_loss_pct) if side == "long" else price * (1 + self.settings.stop_loss_pct)
        take_profit = price * (1 + self.settings.take_profit_pct) if side == "long" else price * (1 - self.settings.take_profit_pct)
        order_id: Optional[str] = None
        if not self.settings.paper_trading:
            response = self.client.place_market_order(
                category=self.settings.category,
                symbol=symbol,
                side="Buy" if side == "long" else "Sell",
                qty=qty,
            )
            order_id = response.get("result", {}).get("orderId")
        self.positions[symbol] = Position(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=self.settings.leverage,
            exchange_order_id=order_id,
        )
        record = TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            mode="paper" if self.settings.paper_trading else "live",
            side=side,
            action="open",
            qty=qty,
            entry_price=price,
            exit_price=price,
            pnl=0.0,
            reason=reason,
        )
        self.recent_trades.insert(0, asdict(record))
        self.recent_trades = self.recent_trades[:50]
        append_jsonl(self.settings.trade_log_path, asdict(record))
        self.logger.info("Opened %s %s at %.4f | %s", side, symbol, price, reason)
        self.notifier.send(build_trade_message(symbol, side, "open", price))

    def _close_position(self, symbol: str, price: float, reason: str) -> None:
        position = self.positions.get(symbol)
        if not position:
            return
        if not self.settings.paper_trading:
            self.client.place_market_order(
                category=self.settings.category,
                symbol=symbol,
                side="Sell" if position.side == "long" else "Buy",
                qty=position.qty,
                reduce_only=True,
            )
        pnl = self._compute_pnl(position, price)
        self.realized_pnl += pnl
        self.cash_balance += pnl
        self.trade_count += 1
        record = TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            mode="paper" if self.settings.paper_trading else "live",
            side=position.side,
            action="close",
            qty=position.qty,
            entry_price=position.entry_price,
            exit_price=price,
            pnl=pnl,
            reason=reason,
        )
        self.recent_trades.insert(0, asdict(record))
        self.recent_trades = self.recent_trades[:50]
        append_jsonl(self.settings.trade_log_path, asdict(record))
        self.positions.pop(symbol, None)
        self.last_closed_at[symbol] = datetime.now(timezone.utc)
        self.logger.info("Closed %s %s at %.4f | PnL %.2f | %s", position.side, symbol, price, pnl, reason)
        self.notifier.send(build_trade_message(symbol, position.side, "close", price, pnl))

    def _compute_pnl(self, position: Position, market_price: float) -> float:
        if position.side == "long":
            return (market_price - position.entry_price) * position.qty
        return (position.entry_price - market_price) * position.qty

    def _mark_to_market(self, symbol: str, price: float) -> None:
        position = self.positions.get(symbol)
        if position:
            position.unrealized_pnl = self._compute_pnl(position, price)

    def _evaluate_risk_exits(self, symbol: str, price: float) -> None:
        position = self.positions.get(symbol)
        if not position:
            return
        if position.side == "long":
            if price <= position.stop_loss:
                self._close_position(symbol, price, "Stop loss")
            elif price >= position.take_profit:
                self._close_position(symbol, price, "Take profit")
        else:
            if price >= position.stop_loss:
                self._close_position(symbol, price, "Stop loss")
            elif price <= position.take_profit:
                self._close_position(symbol, price, "Take profit")

    def _refresh_live_positions(self) -> None:
        if self.settings.paper_trading:
            return
        try:
            live_positions = self.client.get_positions(self.settings.category)
            with self.lock:
                for item in live_positions:
                    symbol = item.get("symbol")
                    size = float(item.get("size", 0) or 0)
                    if symbol in self.positions and size != 0:
                        self.positions[symbol].unrealized_pnl = float(item.get("unrealisedPnl", 0) or 0)
                self.cash_balance = self.client.get_wallet_balance()
        except Exception as exc:
            self.logger.debug("Position refresh skipped: %s", exc)

    def get_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            unrealized = sum(position.unrealized_pnl for position in self.positions.values())
            return {
                "status": self.status,
                "trade_count": self.trade_count,
                "open_positions": [asdict(position) for position in self.positions.values()],
                "realized_pnl": round(self.realized_pnl, 2),
                "unrealized_pnl": round(unrealized, 2),
                "total_pnl": round(self.realized_pnl + unrealized, 2),
                "balance": round(self.cash_balance, 2),
                "recent_trades": self.recent_trades[:20],
                "mode": "paper" if self.settings.paper_trading else "live",
                "symbols": self.active_symbols,
                "configured_symbols": self.settings.symbols,
                "symbol_filter_results": [
                    {
                        "symbol": result.symbol,
                        "passed_filter": result.passed_filter,
                        "filter_reason": result.filter_reason,
                        "profit_factor": result.profit_factor,
                        "max_drawdown_pct": result.max_drawdown_pct,
                        "trades": result.trades,
                        "return_pct": result.total_return_pct,
                    }
                    for result in self.symbol_filter_results
                ],
                "last_update": datetime.now(timezone.utc).isoformat(),
                "last_error": self.last_error,
            }
