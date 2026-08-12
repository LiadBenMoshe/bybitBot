from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pandas as pd

from api import BybitClient, BybitLeverageNotSupported, MarketDataStream, extract_ret_code, is_auth_error
from config import Settings
from costs import build_cost_model
from logger import append_jsonl
from models import PendingEntry, Position, Signal, TradeRecord
from notifier import TelegramNotifier, build_trade_message
from strategy import build_strategy
from backtest import Backtester, BacktestResult


class TraderEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = BybitClient(settings.api_key, settings.api_secret, testnet=settings.testnet)
        self.cost = build_cost_model(settings)
        self.strategy = build_strategy(settings)
        self.market_data = MarketDataStream(
            self.client,
            category=settings.category,
            interval=settings.timeframe,
            poll_interval_seconds=settings.poll_interval_seconds,
        )
        self.notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        self.backtester = Backtester(settings)
        # Reentrant: the connection helpers below take the lock while callers may
        # already hold it.
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.status = "Stopped"
        self.trade_count = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.breakeven_trades = 0
        self.realized_pnl = 0.0
        self.gross_realized_pnl = 0.0
        self.total_fees_paid = 0.0
        self.entries_missed = 0
        self.cash_balance = settings.initial_balance
        self.last_error = ""
        self.consecutive_losses = 0
        self.pause_new_entries_until: Optional[datetime] = None
        self.positions: dict[str, Position] = {}
        self.pending_entries: dict[str, PendingEntry] = {}
        self.recent_trades: list[dict[str, Any]] = []
        self.market_history: dict[str, pd.DataFrame] = {}
        self.last_closed_at: dict[str, datetime] = {}
        self.active_symbols: list[str] = list(settings.symbols)
        self.symbol_filter_results: list[BacktestResult] = []
        self.connection: Dict[str, Any] = {
            "state": "paper" if settings.paper_trading else "unknown",
            "message": "",
            "ret_code": 0,
            "context": "",
            "last_success_at": "",
            "last_failure_at": "",
            "consecutive_failures": 0,
            "balance_is_live": False,
        }
        self._refresh_backoff_until = 0.0

    # ------------------------------------------------------------------
    # exchange connection health
    # ------------------------------------------------------------------
    def _mark_connection_ok(self, *, balance_is_live: Optional[bool] = None) -> None:
        if self.settings.paper_trading:
            return
        with self.lock:
            previous = self.connection["state"]
            self.connection.update(
                state="ok",
                message="",
                ret_code=0,
                context="",
                last_success_at=datetime.now(timezone.utc).isoformat(),
                consecutive_failures=0,
            )
            if balance_is_live is not None:
                self.connection["balance_is_live"] = balance_is_live
        if previous not in {"ok", "unknown"}:
            self.logger.info("Bybit connection recovered (was %s).", previous)

    # Contexts whose failure means the cached balance can no longer be trusted.
    _BALANCE_CONTEXTS = frozenset({"wallet_balance", "position_refresh"})

    def _mark_connection_error(self, exc: Exception, *, context: str) -> None:
        if self.settings.paper_trading:
            return
        auth_failure = is_auth_error(exc)
        state = "auth_failed" if auth_failure else "degraded"
        # A failed set_leverage call says nothing about the balance we already read.
        balance_is_live = False if (auth_failure or context in self._BALANCE_CONTEXTS) else None
        with self.lock:
            previous = self.connection["state"]
            self.connection.update(
                state=state,
                message=str(exc),
                ret_code=extract_ret_code(exc),
                context=context,
                last_failure_at=datetime.now(timezone.utc).isoformat(),
                consecutive_failures=self.connection["consecutive_failures"] + 1,
            )
            if balance_is_live is not None:
                self.connection["balance_is_live"] = balance_is_live
        # Log once per state transition. Repeating the same failure every poll is
        # what turned bot.log into thousands of identical lines.
        if previous != state:
            self.logger.error("Bybit connection %s during %s: %s", state, context, exc)
        else:
            self.logger.debug("Bybit connection still %s during %s: %s", state, context, exc)

    def get_connection_summary(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.connection)

    # ------------------------------------------------------------------
    # startup
    # ------------------------------------------------------------------
    def _sync_fee_rates(self) -> None:
        """Replace configured fee guesses with the account's real tier."""
        if self.settings.paper_trading or not self.settings.auto_sync_fee_rates:
            return
        try:
            maker, taker = self.client.get_fee_rates(self.settings.category, self.active_symbols[0])
        except Exception as exc:
            self.logger.warning("Fee rate sync failed, using configured rates: %s", exc)
            return
        self.cost.maker_fee = maker
        self.cost.taker_fee = taker
        self.strategy.cost.maker_fee = maker
        self.strategy.cost.taker_fee = taker
        self.logger.info(
            "Fee rates synced from exchange: maker %.5f%%, taker %.5f%%, round trip %.4f%%",
            maker * 100,
            taker * 100,
            self.cost.round_trip_pct() * 100,
        )

    def bootstrap(self) -> None:
        self.active_symbols = list(self.settings.symbols)
        for warning in self.settings.sizing_warnings():
            self.logger.warning("Config: %s", warning)
        # Preflight the credentials before the backtest filter (~15s) so a bad key
        # is reported in about a second, and so live mode can never fall back to a
        # simulated balance while pretending to be real.
        preflight_balance: Optional[float] = None
        if not self.settings.paper_trading:
            try:
                preflight_balance = self.client.get_wallet_balance()
            except Exception as exc:
                self._mark_connection_error(exc, context="wallet_balance")
                raise RuntimeError(
                    f"Cannot start in live mode: Bybit rejected the wallet balance request - {exc}. "
                    "Check BYBIT_API_KEY / BYBIT_API_SECRET in .env, or set PAPER_TRADING=true to run simulated."
                ) from exc
            self._mark_connection_ok(balance_is_live=True)
            if preflight_balance == 0.0:
                self.logger.warning("Bybit reports a zero USDT wallet balance. Position sizing will produce no trades.")
        if self.settings.filter_symbols_by_backtest:
            self.symbol_filter_results = self.backtester.run_for_symbols(self.settings.symbols)
            approved = [result.symbol for result in self.symbol_filter_results if result.passed_filter]
            rejected = [result.symbol for result in self.symbol_filter_results if not result.passed_filter]
            if approved:
                self.active_symbols = approved
                self.logger.info("Approved symbols after backtest filter: %s", approved)
            elif self.settings.require_backtest_approval:
                # Falling back to the full basket would make the filter decorative.
                for result in self.symbol_filter_results:
                    self.logger.warning("Backtest filter rejected %s: %s", result.symbol, result.filter_reason)
                raise RuntimeError(
                    f"No validated edge to trade: all {len(self.symbol_filter_results)} symbols failed the "
                    "backtest filter. See logs/bot.log for per-symbol detail, or set "
                    "REQUIRE_BACKTEST_APPROVAL=false to trade anyway."
                )
            else:
                self.logger.warning("No symbols passed the backtest filter. Falling back to configured symbols.")
            if rejected:
                self.logger.info("Rejected symbols after backtest filter: %s", rejected)
        self._sync_fee_rates()
        self.logger.info(
            "Cost model: entry %s (%.4f%%), exit taker (%.4f%%), round trip %.4f%%, volatility floor %.4f%% ATR",
            "maker" if self.cost.entry_is_maker else "taker",
            self.cost.entry_cost_pct() * 100,
            self.cost.exit_cost_pct() * 100,
            self.cost.round_trip_pct() * 100,
            self.strategy.min_atr_pct() * 100,
        )
        for symbol in self.active_symbols:
            frame = self.client.get_kline(self.settings.category, symbol, self.settings.timeframe, limit=250)
            if frame.empty:
                raise RuntimeError(f"No market data returned for {symbol}.")
            self.market_history[symbol] = frame
        if not self.settings.paper_trading:
            self.cash_balance = preflight_balance if preflight_balance is not None else self.cash_balance
            self.logger.info("Live wallet balance: %.2f USDT", self.cash_balance)
            for symbol in self.active_symbols:
                try:
                    self.client.set_leverage(self.settings.category, symbol, self.settings.leverage)
                except BybitLeverageNotSupported as exc:
                    self.logger.info(
                        "Leverage setup skipped for %s because the account is in portfolio margin mode: %s", symbol, exc
                    )
                except Exception as exc:
                    self._mark_connection_error(exc, context="set_leverage")
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
        self._cancel_all_pending("Bot stopped")
        self.market_data.stop()
        self.status = "Stopped"
        if self.thread:
            self.thread.join(timeout=5)

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
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
                self._evaluate_risk_exits(event.symbol, event)
                self._reconcile_pending_entry(event.symbol, event)
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

    # ------------------------------------------------------------------
    # sizing
    # ------------------------------------------------------------------
    def _calculate_position_size(self, price: float, stop_loss: float | None = None) -> float:
        risk_amount = self.cash_balance * self.settings.risk_per_trade
        if stop_loss is not None:
            stop_distance = max(abs(price - stop_loss), 0.0001)
        else:
            stop_distance = max(price * self.settings.stop_loss_pct, 0.0001)
        risk_based_qty = risk_amount / stop_distance
        max_notional = self.cash_balance * self.settings.max_position_notional_pct
        max_notional_qty = max_notional / max(price, 0.0001)
        if risk_based_qty > max_notional_qty:
            self.logger.warning(
                "Notional cap bound the position: risk sizing wanted %.6f but MAX_POSITION_NOTIONAL_PCT allows %.6f. "
                "RISK_PER_TRADE is not governing this trade.",
                risk_based_qty,
                max_notional_qty,
            )
        return round(min(risk_based_qty, max_notional_qty), 6)

    # ------------------------------------------------------------------
    # signal handling
    # ------------------------------------------------------------------
    def _open_slots_taken(self) -> int:
        return len(self.positions) + len(self.pending_entries)

    def _process_signal(self, signal: Signal) -> None:
        with self.lock:
            existing = self.positions.get(signal.symbol)
            if signal.action == "hold":
                return
            if not self.settings.paper_trading and self.connection["state"] == "auth_failed":
                # Open positions are still managed from local klines; only new
                # exchange writes are suppressed.
                self.logger.info("Entry skipped for %s: exchange connection unavailable.", signal.symbol)
                return
            if self._is_entry_pause_active(signal.timestamp):
                return
            action = signal.action
            if self._is_in_cooldown(signal.symbol, signal.timestamp):
                return
            if existing and ((existing.side == "long" and action == "buy") or (existing.side == "short" and action == "sell")):
                return
            if signal.symbol in self.pending_entries:
                return
            if existing:
                self._close_position(signal.symbol, signal.price, f"Signal flip: {signal.reason}")
            if self._open_slots_taken() >= self.settings.max_positions:
                return
            side = "long" if action == "buy" else "short"
            qty = self._calculate_position_size(signal.price, signal.stop_loss)
            if qty <= 0:
                return
            stop_loss, take_profit = self._resolve_exit_levels(side, signal)
            entry_atr = self._latest_atr(signal.symbol)
            if self.settings.maker_entry_enabled:
                self._queue_maker_entry(signal, side, qty, stop_loss, take_profit, entry_atr)
            else:
                self._open_position(
                    signal.symbol, side, signal.price, qty, signal.reason, stop_loss, take_profit, False, entry_atr
                )

    def _resolve_exit_levels(self, side: str, signal: Signal) -> tuple[float, float]:
        stop_loss = signal.stop_loss
        take_profit = signal.take_profit
        price = signal.price
        if stop_loss is None:
            stop_loss = price * (1 - self.settings.stop_loss_pct) if side == "long" else price * (1 + self.settings.stop_loss_pct)
        if take_profit is None:
            take_profit = price * (1 + self.settings.take_profit_pct) if side == "long" else price * (1 - self.settings.take_profit_pct)
        return stop_loss, take_profit

    def _latest_atr(self, symbol: str) -> float:
        frame = self.market_history.get(symbol)
        if frame is None or frame.empty or len(frame) < 50:
            return 0.0
        try:
            enriched = self.strategy.apply_indicators(frame)
            value = enriched.iloc[-1]["atr"]
            return 0.0 if pd.isna(value) else float(value)
        except Exception:
            return 0.0

    def _is_in_cooldown(self, symbol: str, timestamp: datetime) -> bool:
        if self.settings.cooldown_bars <= 0:
            return False
        last_closed = self.last_closed_at.get(symbol)
        if last_closed is None:
            return False
        return (timestamp - last_closed).total_seconds() < self.settings.cooldown_bars * self._minutes_per_bar() * 60

    def _minutes_per_bar(self) -> int:
        try:
            return int(self.settings.timeframe)
        except ValueError:
            return 15

    def _is_entry_pause_active(self, timestamp: datetime) -> bool:
        return self.pause_new_entries_until is not None and timestamp < self.pause_new_entries_until

    # ------------------------------------------------------------------
    # post-only entries
    # ------------------------------------------------------------------
    def _maker_limit_price(self, symbol: str, side: str, reference_price: float) -> float:
        """Price a resting order so it joins the book instead of crossing it."""
        price = reference_price
        if not self.settings.paper_trading:
            try:
                bid, ask = self.client.get_best_bid_ask(self.settings.category, symbol)
                price = min(bid, reference_price) if side == "long" else max(ask, reference_price)
            except Exception as exc:
                self.logger.warning("Book fetch failed for %s, resting at last close: %s", symbol, exc)
            try:
                return self.client.normalize_price(
                    self.settings.category, symbol, price, "Buy" if side == "long" else "Sell"
                )
            except Exception as exc:
                self.logger.warning("Price normalization failed for %s: %s", symbol, exc)
        return price

    def _queue_maker_entry(
        self,
        signal: Signal,
        side: str,
        qty: float,
        stop_loss: float,
        take_profit: float,
        entry_atr: float,
    ) -> None:
        symbol = signal.symbol
        limit_price = self._maker_limit_price(symbol, side, signal.price)
        order_id: Optional[str] = None
        normalized_qty = qty
        if not self.settings.paper_trading:
            try:
                normalized_qty = self.client.normalize_order_qty(self.settings.category, symbol, qty)
                response = self.client.place_limit_order(
                    category=self.settings.category,
                    symbol=symbol,
                    side="Buy" if side == "long" else "Sell",
                    qty=normalized_qty,
                    price=limit_price,
                    post_only=True,
                )
                order_id = response.get("result", {}).get("orderId")
            except Exception as exc:
                self.logger.warning("Post-only entry rejected for %s: %s", symbol, exc)
                return
        self.pending_entries[symbol] = PendingEntry(
            symbol=symbol,
            side=side,
            limit_price=limit_price,
            qty=normalized_qty,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=signal.reason,
            entry_atr=entry_atr,
            exchange_order_id=order_id,
        )
        self.logger.info(
            "Resting %s entry for %s at %.6f (qty %.8f) | %s", side, symbol, limit_price, normalized_qty, signal.reason
        )

    def _reconcile_pending_entry(self, symbol: str, event: Any) -> None:
        with self.lock:
            pending = self.pending_entries.get(symbol)
            if not pending:
                return
            if symbol in self.positions:
                self._cancel_pending(pending, "Position already open")
                return

            filled_price = self._pending_fill_price(pending, event)
            if filled_price is not None:
                self.pending_entries.pop(symbol, None)
                self._open_position(
                    symbol,
                    pending.side,
                    filled_price,
                    pending.qty,
                    pending.reason,
                    pending.stop_loss,
                    pending.take_profit,
                    True,
                    pending.entry_atr,
                )
                return

            pending.bars_waited += 1
            chase = abs(event.close - pending.limit_price) / max(pending.limit_price, 1e-9)
            expired = pending.bars_waited > self.settings.maker_entry_timeout_bars
            if not expired and chase <= self.settings.maker_max_chase_pct:
                return
            self._cancel_pending(pending, "Timed out" if expired else "Price ran away")
            if self.settings.maker_entry_fallback == "market":
                self._open_position(
                    symbol,
                    pending.side,
                    event.close,
                    pending.qty,
                    f"{pending.reason} (market fallback)",
                    pending.stop_loss,
                    pending.take_profit,
                    False,
                    pending.entry_atr,
                )
            else:
                self.entries_missed += 1

    def _pending_fill_price(self, pending: PendingEntry, event: Any) -> Optional[float]:
        if self.settings.paper_trading:
            # Same predicate the backtester uses, so paper and simulated fills agree.
            if pending.side == "long" and event.low < pending.limit_price:
                return pending.limit_price
            if pending.side == "short" and event.high > pending.limit_price:
                return pending.limit_price
            return None
        if not pending.exchange_order_id:
            return None
        try:
            resting = self.client.get_open_orders(self.settings.category, pending.symbol)
            if any(order.get("orderId") == pending.exchange_order_id for order in resting):
                return None
            status = self.client.get_order_status(self.settings.category, pending.symbol, pending.exchange_order_id)
            if status.get("orderStatus") == "Filled":
                avg_price = float(status.get("avgPrice") or pending.limit_price)
                filled_qty = float(status.get("cumExecQty") or pending.qty)
                pending.qty = filled_qty or pending.qty
                return avg_price
        except Exception as exc:
            self.logger.warning("Pending entry reconciliation failed for %s: %s", pending.symbol, exc)
        return None

    def _cancel_pending(self, pending: PendingEntry, reason: str) -> None:
        self.pending_entries.pop(pending.symbol, None)
        if not self.settings.paper_trading and pending.exchange_order_id:
            try:
                self.client.cancel_order(self.settings.category, pending.symbol, pending.exchange_order_id)
            except Exception as exc:
                self.logger.warning("Cancel failed for %s: %s", pending.symbol, exc)
        self.logger.info("Pending %s entry for %s dropped: %s", pending.side, pending.symbol, reason)

    def _cancel_all_pending(self, reason: str) -> None:
        with self.lock:
            for pending in list(self.pending_entries.values()):
                self._cancel_pending(pending, reason)

    # ------------------------------------------------------------------
    # positions
    # ------------------------------------------------------------------
    def _open_position(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        reason: str,
        stop_loss: float,
        take_profit: float,
        is_maker: bool,
        entry_atr: float,
    ) -> None:
        order_id: Optional[str] = None
        normalized_qty = qty
        if not self.settings.paper_trading and not is_maker:
            normalized_qty = self.client.normalize_order_qty(self.settings.category, symbol, qty)
            response = self.client.place_market_order(
                category=self.settings.category,
                symbol=symbol,
                side="Buy" if side == "long" else "Sell",
                qty=normalized_qty,
            )
            order_id = response.get("result", {}).get("orderId")
        entry_cost = self.cost.with_entry_style(is_maker)
        entry_fee = entry_cost.entry_fee(price * normalized_qty)
        self.cash_balance -= entry_fee
        self.total_fees_paid += entry_fee
        self.positions[symbol] = Position(
            symbol=symbol,
            side=side,
            qty=normalized_qty,
            entry_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=self.settings.leverage,
            exchange_order_id=order_id,
            peak_price=price,
            trough_price=price,
            entry_fee=entry_fee,
            entry_is_maker=is_maker,
            entry_atr=entry_atr,
        )
        record = TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            mode="paper" if self.settings.paper_trading else "live",
            side=side,
            action="open",
            qty=normalized_qty,
            entry_price=price,
            exit_price=price,
            pnl=0.0,
            reason=reason,
            gross_pnl=0.0,
            fees=entry_fee,
            entry_is_maker=is_maker,
        )
        self.recent_trades.insert(0, asdict(record))
        self.recent_trades = self.recent_trades[:50]
        append_jsonl(self.settings.trade_log_path, asdict(record))
        self.logger.info(
            "Opened %s %s at %.6f qty %.8f (%s entry, fee %.4f) | %s",
            side,
            symbol,
            price,
            normalized_qty,
            "maker" if is_maker else "taker",
            entry_fee,
            reason,
        )
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
        gross, fees, net = self.cost.net_pnl(
            position.side, position.entry_price, price, position.qty, entry_fee=position.entry_fee
        )
        exit_fee = fees - position.entry_fee
        # The entry fee was already charged to cash at open; only the exit fee is new.
        self.realized_pnl += net
        self.gross_realized_pnl += gross
        self.total_fees_paid += exit_fee
        self.cash_balance += gross - exit_fee
        self.trade_count += 1
        if net > 0:
            self.winning_trades += 1
            self.consecutive_losses = 0
        elif net < 0:
            self.losing_trades += 1
            self.consecutive_losses += 1
        else:
            self.breakeven_trades += 1
            self.consecutive_losses = 0
        if (
            net < 0
            and self.settings.max_consecutive_losses > 0
            and self.consecutive_losses >= self.settings.max_consecutive_losses
        ):
            pause_minutes = self.settings.cooldown_after_loss_bars * self._minutes_per_bar()
            self.pause_new_entries_until = datetime.now(timezone.utc) + timedelta(minutes=pause_minutes)
            self.logger.info(
                "Pausing new entries until %s after %s consecutive losses.",
                self.pause_new_entries_until.isoformat(),
                self.consecutive_losses,
            )
        record = TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            mode="paper" if self.settings.paper_trading else "live",
            side=position.side,
            action="close",
            qty=position.qty,
            entry_price=position.entry_price,
            exit_price=price,
            pnl=net,
            reason=reason,
            gross_pnl=gross,
            fees=fees,
            entry_is_maker=position.entry_is_maker,
        )
        self.recent_trades.insert(0, asdict(record))
        self.recent_trades = self.recent_trades[:50]
        append_jsonl(self.settings.trade_log_path, asdict(record))
        self.positions.pop(symbol, None)
        self.last_closed_at[symbol] = datetime.now(timezone.utc)
        self.logger.info(
            "Closed %s %s at %.6f | gross %.2f fees %.2f net %.2f | %s",
            position.side,
            symbol,
            price,
            gross,
            fees,
            net,
            reason,
        )
        self.notifier.send(build_trade_message(symbol, position.side, "close", price, net))

    def _compute_pnl(self, position: Position, market_price: float) -> float:
        """Unrealized net PnL: gross move less the fees already paid and still owed."""
        gross = self.cost.gross_pnl(position.side, position.entry_price, market_price, position.qty)
        exit_fee = self.cost.exit_fee(market_price * position.qty)
        return gross - position.entry_fee - exit_fee

    def _mark_to_market(self, symbol: str, price: float) -> None:
        position = self.positions.get(symbol)
        if position:
            position.unrealized_pnl = self._compute_pnl(position, price)
            position.peak_price = max(position.peak_price, price)
            position.trough_price = min(position.trough_price, price)

    def _update_protective_stop(self, position: Position) -> None:
        round_trip = self.cost.round_trip_pct()
        trigger_pct = self.settings.break_even_trigger_pct
        if not position.break_even_armed and trigger_pct > 0:
            # The offset must clear the round trip, or "break even" is a guaranteed loss.
            offset_pct = round_trip + self.settings.break_even_offset_pct
            if position.side == "long":
                favorable_move = (position.peak_price - position.entry_price) / max(position.entry_price, 0.0001)
                if favorable_move >= trigger_pct:
                    position.stop_loss = max(position.stop_loss, position.entry_price * (1 + offset_pct))
                    position.break_even_armed = True
            else:
                favorable_move = (position.entry_price - position.trough_price) / max(position.entry_price, 0.0001)
                if favorable_move >= trigger_pct:
                    position.stop_loss = min(position.stop_loss, position.entry_price * (1 - offset_pct))
                    position.break_even_armed = True

        if self.settings.take_profit_mode == "trail" and position.break_even_armed and position.entry_atr > 0:
            distance = position.entry_atr * self.settings.trail_atr_multiple
            if position.side == "long":
                position.stop_loss = max(position.stop_loss, position.peak_price - distance)
            else:
                position.stop_loss = min(position.stop_loss, position.trough_price + distance)
            position.trail_stop = position.stop_loss

    def _evaluate_risk_exits(self, symbol: str, event: Any) -> None:
        position = self.positions.get(symbol)
        if not position:
            return
        high = float(getattr(event, "high", event.close))
        low = float(getattr(event, "low", event.close))
        use_target = self.settings.take_profit_mode != "trail"
        # Stop is tested before target: when one bar spans both, assume the worse fill.
        if position.side == "long":
            if low <= position.stop_loss:
                self._close_position(symbol, position.stop_loss, "Stop loss")
                return
            if use_target and high >= position.take_profit:
                self._close_position(symbol, position.take_profit, "Take profit")
                return
        else:
            if high >= position.stop_loss:
                self._close_position(symbol, position.stop_loss, "Stop loss")
                return
            if use_target and low <= position.take_profit:
                self._close_position(symbol, position.take_profit, "Take profit")
                return
        position.bars_held += 1
        self._update_protective_stop(position)

    def _refresh_live_positions(self) -> None:
        if self.settings.paper_trading:
            return
        if time.monotonic() < self._refresh_backoff_until:
            return
        # Both calls stay outside the lock: they can block for seconds while pybit
        # retries, and snapshot requests from the web thread need the lock meanwhile.
        try:
            live_positions = self.client.get_positions(self.settings.category)
            balance = self.client.get_wallet_balance()
        except Exception as exc:
            self._mark_connection_error(exc, context="position_refresh")
            if is_auth_error(exc):
                # An expired key will not heal on its own; stop hammering the API.
                self._refresh_backoff_until = time.monotonic() + 300.0
            else:
                failures = self.connection["consecutive_failures"]
                self._refresh_backoff_until = time.monotonic() + min(5.0 * 2 ** failures, 120.0)
            return
        with self.lock:
            for item in live_positions:
                symbol = item.get("symbol")
                size = float(item.get("size", 0) or 0)
                if symbol in self.positions and size != 0:
                    self.positions[symbol].unrealized_pnl = float(item.get("unrealisedPnl", 0) or 0)
            self.cash_balance = balance
        self._refresh_backoff_until = 0.0
        self._mark_connection_ok(balance_is_live=True)

    def close_open_position(self, symbol: str, reason: str = "Manual close") -> dict[str, Any]:
        normalized_symbol = symbol.strip().upper()
        with self.lock:
            position = self.positions.get(normalized_symbol)
            if not position:
                raise ValueError(f"No open position for {normalized_symbol}.")
            frame = self.market_history.get(normalized_symbol, pd.DataFrame())
            if not frame.empty and "close" in frame.columns:
                market_price = float(frame.iloc[-1]["close"])
            else:
                latest_frame = self.client.get_kline(
                    self.settings.category, normalized_symbol, self.settings.timeframe, limit=1
                )
                if latest_frame.empty:
                    raise RuntimeError(f"No market price available for {normalized_symbol}.")
                market_price = float(latest_frame.iloc[-1]["close"])
                self.market_history[normalized_symbol] = latest_frame
            self._close_position(normalized_symbol, market_price, reason)
            return {
                "symbol": normalized_symbol,
                "price": market_price,
                "status": "closed",
            }

    # ------------------------------------------------------------------
    # snapshots
    # ------------------------------------------------------------------
    def _cost_summary(self) -> Dict[str, Any]:
        gross_total = abs(self.gross_realized_pnl)
        return {
            "total_fees_paid": round(self.total_fees_paid, 4),
            "gross_realized_pnl": round(self.gross_realized_pnl, 2),
            "fee_drag_pct": round(self.total_fees_paid / gross_total * 100, 2) if gross_total else 0.0,
            "round_trip_cost_pct": round(self.cost.round_trip_pct() * 100, 4),
            "entry_style": "maker" if self.cost.entry_is_maker else "taker",
            "entries_missed": self.entries_missed,
        }

    def get_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            unrealized = sum(position.unrealized_pnl for position in self.positions.values())
            success_rate = (self.winning_trades / self.trade_count * 100) if self.trade_count else 0.0
            return {
                "status": self.status,
                "trade_count": self.trade_count,
                "winning_trades": self.winning_trades,
                "losing_trades": self.losing_trades,
                "breakeven_trades": self.breakeven_trades,
                "success_rate_pct": round(success_rate, 2),
                "consecutive_losses": self.consecutive_losses,
                "entry_pause_until": self.pause_new_entries_until.isoformat() if self.pause_new_entries_until else "",
                "open_positions": [asdict(position) for position in self.positions.values()],
                "pending_entries": [asdict(pending) for pending in self.pending_entries.values()],
                "realized_pnl": round(self.realized_pnl, 2),
                "unrealized_pnl": round(unrealized, 2),
                "total_pnl": round(self.realized_pnl + unrealized, 2),
                "balance": round(self.cash_balance, 2),
                "balance_is_live": self.connection["balance_is_live"],
                "connection": dict(self.connection),
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
                        "expectancy_r": result.expectancy_r,
                        "total_fees": result.total_fees,
                    }
                    for result in self.symbol_filter_results
                ],
                "last_update": datetime.now(timezone.utc).isoformat(),
                "last_error": self.last_error,
                **self._cost_summary(),
            }

    def get_compact_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            unrealized = sum(position.unrealized_pnl for position in self.positions.values())
            success_rate = (self.winning_trades / self.trade_count * 100) if self.trade_count else 0.0
            return {
                "status": self.status,
                "trade_count": self.trade_count,
                "winning_trades": self.winning_trades,
                "losing_trades": self.losing_trades,
                "breakeven_trades": self.breakeven_trades,
                "success_rate_pct": round(success_rate, 2),
                "consecutive_losses": self.consecutive_losses,
                "entry_pause_until": self.pause_new_entries_until.isoformat() if self.pause_new_entries_until else "",
                "open_position_count": len(self.positions),
                "pending_entry_count": len(self.pending_entries),
                "realized_pnl": round(self.realized_pnl, 2),
                "unrealized_pnl": round(unrealized, 2),
                "total_pnl": round(self.realized_pnl + unrealized, 2),
                "balance": round(self.cash_balance, 2),
                "balance_is_live": self.connection["balance_is_live"],
                "connection": dict(self.connection),
                "last_update": datetime.now(timezone.utc).isoformat(),
                "last_error": self.last_error,
                **self._cost_summary(),
            }
