from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Any, Callable, Dict, Optional

import pandas as pd
from pybit.unified_trading import HTTP, WebSocket


class RateLimiter:
    def __init__(self, max_calls: int, period_seconds: float) -> None:
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            while self.calls and now - self.calls[0] > self.period_seconds:
                self.calls.popleft()
            if len(self.calls) >= self.max_calls:
                delay = self.period_seconds - (now - self.calls[0])
                if delay > 0:
                    time.sleep(delay)
            self.calls.append(time.monotonic())


class BybitAPIError(RuntimeError):
    pass


class BybitLeverageNotSupported(BybitAPIError):
    pass


@dataclass(slots=True)
class KlineEvent:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    confirm: bool


class BybitClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.rate_limiter = RateLimiter(max_calls=8, period_seconds=1)
        self.http = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)
        self.testnet = testnet
        self.instrument_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def _request(self, func: Callable[..., Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            self.rate_limiter.wait()
            try:
                result = func(**kwargs)
                if result.get("retCode", 0) != 0:
                    raise BybitAPIError(result.get("retMsg", "Unknown Bybit error"))
                return result
            except Exception as exc:
                last_error = exc
                self.logger.warning("Bybit request attempt %s failed: %s", attempt, exc)
                time.sleep(0.6 * attempt)
        raise BybitAPIError(str(last_error))

    def get_wallet_balance(self, account_type: str = "UNIFIED", coin: str = "USDT") -> float:
        result = self._request(self.http.get_wallet_balance, accountType=account_type, coin=coin)
        balances = result.get("result", {}).get("list", [])
        if not balances:
            return 0.0
        for entry in balances[0].get("coin", []):
            if entry.get("coin") == coin:
                return float(entry.get("walletBalance", 0.0))
        return 0.0

    def get_positions(self, category: str, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        kwargs: Dict[str, Any] = {"category": category}
        if symbol:
            kwargs["symbol"] = symbol
        elif category == "linear":
            kwargs["settleCoin"] = "USDT"
        elif category == "inverse":
            kwargs["settleCoin"] = "BTC"
        result = self._request(self.http.get_positions, **kwargs)
        return result.get("result", {}).get("list", [])

    def get_kline(
        self,
        category: str,
        symbol: str,
        interval: str,
        limit: int = 200,
        end: Optional[int] = None,
    ) -> pd.DataFrame:
        kwargs: Dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if end is not None:
            kwargs["end"] = end
        result = self._request(self.http.get_kline, **kwargs)
        rows = result.get("result", {}).get("list", [])
        if not rows:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])

        if isinstance(rows[0], dict):
            frame = pd.DataFrame(rows).rename(
                columns={
                    "start": "start_time",
                }
            )
        else:
            frame = pd.DataFrame(
                rows,
                columns=["start_time", "open", "high", "low", "close", "volume", "turnover"],
            )

        required_columns = ["start_time", "open", "high", "low", "close", "volume", "turnover"]
        missing_columns = [column for column in required_columns if column not in frame.columns]
        if missing_columns:
            self.logger.warning("Kline response for %s missing columns: %s", symbol, ", ".join(missing_columns))
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])

        for column in ["open", "high", "low", "close", "volume", "turnover"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["timestamp"] = pd.to_datetime(frame["start_time"].astype("int64"), unit="ms", utc=True)
        frame = frame.sort_values("timestamp").reset_index(drop=True)
        return frame[["timestamp", "open", "high", "low", "close", "volume", "turnover"]]

    def get_kline_history(self, category: str, symbol: str, interval: str, total: int) -> pd.DataFrame:
        """Fetch up to `total` candles, paging backwards past the 1000-per-request cap."""
        frames: list[pd.DataFrame] = []
        collected = 0
        end_ms: Optional[int] = None
        while collected < total:
            batch_size = min(1000, total - collected)
            kwargs: Dict[str, Any] = {
                "category": category,
                "symbol": symbol,
                "interval": interval,
                "limit": batch_size,
            }
            if end_ms is not None:
                kwargs["end"] = end_ms
            frame = self.get_kline(**kwargs)
            if frame.empty:
                break
            frames.append(frame)
            collected += len(frame)
            oldest = frame["timestamp"].iloc[0]
            next_end = int(oldest.value // 1_000_000) - 1
            if end_ms is not None and next_end >= end_ms:
                break
            end_ms = next_end
            if len(frame) < batch_size:
                break
        if not frames:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")
        return combined.reset_index(drop=True).tail(total).reset_index(drop=True)

    def get_instrument_info(self, category: str, symbol: str) -> dict[str, Any]:
        cache_key = (category, symbol)
        cached = self.instrument_cache.get(cache_key)
        if cached is not None:
            return cached
        result = self._request(self.http.get_instruments_info, category=category, symbol=symbol)
        items = result.get("result", {}).get("list", [])
        if not items:
            raise BybitAPIError(f"No instrument info returned for {symbol}.")
        self.instrument_cache[cache_key] = items[0]
        return items[0]

    def normalize_order_qty(self, category: str, symbol: str, qty: float) -> float:
        if category == "spot":
            return qty
        instrument = self.get_instrument_info(category, symbol)
        lot_size_filter = instrument.get("lotSizeFilter", {})
        qty_step = lot_size_filter.get("qtyStep")
        min_order_qty = lot_size_filter.get("minOrderQty")
        max_order_qty = lot_size_filter.get("maxOrderQty")

        normalized = Decimal(str(qty))
        if qty_step:
            step = Decimal(str(qty_step))
            if step > 0:
                normalized = (normalized / step).to_integral_value(rounding=ROUND_DOWN) * step
        if min_order_qty:
            min_qty = Decimal(str(min_order_qty))
            if normalized < min_qty:
                normalized = min_qty
        if max_order_qty:
            max_qty = Decimal(str(max_order_qty))
            if normalized > max_qty:
                normalized = max_qty
        if normalized <= 0:
            raise BybitAPIError(f"Normalized quantity for {symbol} is not tradable.")
        return float(normalized)

    def place_market_order(
        self,
        category: str,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        normalized_qty = self.normalize_order_qty(category, symbol, qty)
        return self._request(
            self.http.place_order,
            category=category,
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=_decimal_to_string(normalized_qty),
            reduceOnly=reduce_only,
            timeInForce="IOC",
        )

    def normalize_price(self, category: str, symbol: str, price: float, side: str) -> float:
        """Round a limit price to the instrument tick size.

        Buys round down and sells round up, so a post-only order is never pushed
        across the spread by rounding. Bybit rejects prices off the tick grid.
        """
        instrument = self.get_instrument_info(category, symbol)
        tick_size = instrument.get("priceFilter", {}).get("tickSize")
        value = Decimal(str(price))
        if tick_size:
            step = Decimal(str(tick_size))
            if step > 0:
                rounding = ROUND_DOWN if side.lower() == "buy" else ROUND_UP
                value = (value / step).to_integral_value(rounding=rounding) * step
        if value <= 0:
            raise BybitAPIError(f"Normalized price for {symbol} is not tradable.")
        return float(value)

    def get_best_bid_ask(self, category: str, symbol: str) -> tuple[float, float]:
        result = self._request(self.http.get_tickers, category=category, symbol=symbol)
        items = result.get("result", {}).get("list", [])
        if not items:
            raise BybitAPIError(f"No ticker data returned for {symbol}.")
        item = items[0]
        bid = float(item.get("bid1Price") or 0.0)
        ask = float(item.get("ask1Price") or 0.0)
        if bid <= 0 or ask <= 0:
            raise BybitAPIError(f"Incomplete book for {symbol}.")
        return bid, ask

    def place_limit_order(
        self,
        category: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        post_only: bool = True,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        normalized_qty = self.normalize_order_qty(category, symbol, qty)
        normalized_price = self.normalize_price(category, symbol, price, side)
        return self._request(
            self.http.place_order,
            category=category,
            symbol=symbol,
            side=side,
            orderType="Limit",
            qty=_decimal_to_string(normalized_qty),
            price=_decimal_to_string(normalized_price),
            reduceOnly=reduce_only,
            timeInForce="PostOnly" if post_only else "GTC",
        )

    def cancel_order(self, category: str, symbol: str, order_id: str) -> dict[str, Any]:
        return self._request(self.http.cancel_order, category=category, symbol=symbol, orderId=order_id)

    def get_open_orders(self, category: str, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        kwargs: Dict[str, Any] = {"category": category}
        if symbol:
            kwargs["symbol"] = symbol
        elif category == "linear":
            kwargs["settleCoin"] = "USDT"
        result = self._request(self.http.get_open_orders, **kwargs)
        return result.get("result", {}).get("list", [])

    def get_order_status(self, category: str, symbol: str, order_id: str) -> dict[str, Any]:
        """Terminal state of an order that is no longer resting on the book."""
        result = self._request(self.http.get_order_history, category=category, symbol=symbol, orderId=order_id)
        items = result.get("result", {}).get("list", [])
        return items[0] if items else {}

    def get_fee_rates(self, category: str, symbol: Optional[str] = None) -> tuple[float, float]:
        """Return (maker, taker) for the authenticated account's actual fee tier."""
        kwargs: Dict[str, Any] = {"category": category}
        if symbol:
            kwargs["symbol"] = symbol
        result = self._request(self.http.get_fee_rates, **kwargs)
        items = result.get("result", {}).get("list", [])
        if not items:
            raise BybitAPIError("No fee rate data returned.")
        entry = items[0]
        return float(entry.get("makerFeeRate", 0.0)), float(entry.get("takerFeeRate", 0.0))

    def set_leverage(self, category: str, symbol: str, leverage: int) -> None:
        if category == "spot":
            return
        try:
            self._request(
                self.http.set_leverage,
                category=category,
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except BybitAPIError as exc:
            if "pm mode cannot set leverage" in str(exc).lower():
                raise BybitLeverageNotSupported(str(exc)) from exc
            raise


def _decimal_to_string(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


class MarketDataStream:
    def __init__(self, client: BybitClient, category: str, interval: str, poll_interval_seconds: int = 15) -> None:
        self.client = client
        self.category = category
        self.interval = interval
        self.poll_interval_seconds = poll_interval_seconds
        self.logger = logging.getLogger(self.__class__.__name__)
        self.events: "Queue[KlineEvent]" = Queue()
        self.ws: Optional[WebSocket] = None
        self.stop_event = threading.Event()
        self.poll_threads: list[threading.Thread] = []
        self.watchdog_thread: Optional[threading.Thread] = None
        self.symbols: list[str] = []
        self.last_message_at = time.monotonic()
        self.polling_active = False

    def _extract_symbol(self, message: dict[str, Any], item: dict[str, Any]) -> Optional[str]:
        symbol = item.get("symbol")
        if symbol:
            return str(symbol)
        topic = message.get("topic", "")
        if isinstance(topic, str) and topic:
            parts = topic.split(".")
            if parts:
                return parts[-1]
        return None

    def _handle_ws_message(self, message: dict[str, Any]) -> None:
        self.last_message_at = time.monotonic()
        data = message.get("data", [])
        if isinstance(data, dict):
            data = [data]
        for item in data:
            try:
                symbol = self._extract_symbol(message, item)
                if not symbol:
                    raise KeyError("symbol")
                self.events.put(
                    KlineEvent(
                        symbol=symbol,
                        timestamp=datetime.fromtimestamp(int(item["start"]) / 1000, tz=timezone.utc),
                        open=float(item["open"]),
                        high=float(item["high"]),
                        low=float(item["low"]),
                        close=float(item["close"]),
                        volume=float(item["volume"]),
                        confirm=bool(item.get("confirm", False)),
                    )
                )
            except Exception as exc:
                self.logger.warning("WebSocket parse failure: %s", exc)

    def start(self, symbols: list[str]) -> None:
        self.stop_event.clear()
        self.symbols = list(symbols)
        self.last_message_at = time.monotonic()
        self.polling_active = False
        try:
            channel_type = "linear" if self.category != "spot" else "spot"
            self.ws = WebSocket(testnet=self.client.testnet, channel_type=channel_type)
            for symbol in symbols:
                self.ws.kline_stream(interval=int(self.interval), symbol=symbol, callback=self._handle_ws_message)
            self.logger.info("WebSocket stream started for %s", symbols)
        except Exception as exc:
            self.logger.warning("WebSocket unavailable, using polling fallback: %s", exc)
            self.ws = None
            self._start_polling(symbols)
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def _start_polling(self, symbols: list[str]) -> None:
        if self.polling_active:
            return
        self.polling_active = True
        self.poll_threads = []
        for symbol in symbols:
            thread = threading.Thread(target=self._poll_symbol, args=(symbol,), daemon=True)
            self.poll_threads.append(thread)
            thread.start()
        self.logger.info("Polling fallback started for %s", symbols)

    def _watchdog_loop(self) -> None:
        stale_after = max(45, self.poll_interval_seconds * 3)
        while not self.stop_event.wait(5):
            if self.polling_active:
                continue
            if time.monotonic() - self.last_message_at > stale_after:
                self.logger.warning("WebSocket stale or disconnected. Switching to polling fallback.")
                self._start_polling(self.symbols)
                return

    def _poll_symbol(self, symbol: str) -> None:
        last_timestamp: Optional[pd.Timestamp] = None
        while not self.stop_event.is_set():
            try:
                frame = self.client.get_kline(self.category, symbol, self.interval, limit=2)
                if not frame.empty:
                    row = frame.iloc[-1]
                    timestamp = row["timestamp"]
                    if last_timestamp is None or timestamp > last_timestamp:
                        last_timestamp = timestamp
                        self.events.put(
                            KlineEvent(
                                symbol=symbol,
                                timestamp=timestamp.to_pydatetime(),
                                open=float(row["open"]),
                                high=float(row["high"]),
                                low=float(row["low"]),
                                close=float(row["close"]),
                                volume=float(row["volume"]),
                                confirm=True,
                            )
                        )
            except Exception as exc:
                self.logger.warning("Polling failed for %s: %s", symbol, exc)
            self.stop_event.wait(self.poll_interval_seconds)

    def get_event(self, timeout: float = 1.0) -> Optional[KlineEvent]:
        try:
            return self.events.get(timeout=timeout)
        except Empty:
            return None

    def stop(self) -> None:
        self.stop_event.set()
        if self.ws:
            try:
                self.ws.exit()
            except Exception:
                pass
