from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
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

    def get_kline(self, category: str, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        result = self._request(
            self.http.get_kline,
            category=category,
            symbol=symbol,
            interval=interval,
            limit=limit,
        )
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
