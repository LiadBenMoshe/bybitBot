from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _split_symbols(raw: str) -> List[str]:
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


@dataclass(slots=True)
class Settings:
    api_key: str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("BYBIT_API_SECRET", ""))
    testnet: bool = field(default_factory=lambda: os.getenv("BYBIT_TESTNET", "true").lower() == "true")
    paper_trading: bool = field(default_factory=lambda: os.getenv("PAPER_TRADING", "true").lower() == "true")
    symbols: List[str] = field(default_factory=lambda: _split_symbols(os.getenv("TRADING_SYMBOLS", "BTCUSDT,ETHUSDT")))
    category: str = field(default_factory=lambda: os.getenv("BYBIT_CATEGORY", "linear"))
    timeframe: str = field(default_factory=lambda: os.getenv("TIMEFRAME", "15"))
    initial_balance: float = field(default_factory=lambda: float(os.getenv("INITIAL_BALANCE", "10000")))
    risk_per_trade: float = field(default_factory=lambda: float(os.getenv("RISK_PER_TRADE", "0.01")))
    leverage: int = field(default_factory=lambda: int(os.getenv("DEFAULT_LEVERAGE", "2")))
    stop_loss_pct: float = field(default_factory=lambda: float(os.getenv("STOP_LOSS_PCT", "0.01")))
    take_profit_pct: float = field(default_factory=lambda: float(os.getenv("TAKE_PROFIT_PCT", "0.02")))
    max_positions: int = field(default_factory=lambda: int(os.getenv("MAX_OPEN_POSITIONS", "3")))
    poll_interval_seconds: int = field(default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "15")))
    log_dir: Path = field(default_factory=lambda: Path(os.getenv("LOG_DIR", "logs")))
    trade_log_file: str = field(default_factory=lambda: os.getenv("TRADE_LOG_FILE", "trades.jsonl"))
    event_log_file: str = field(default_factory=lambda: os.getenv("EVENT_LOG_FILE", "bot.log"))
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    auth_enabled: bool = field(default_factory=lambda: os.getenv("AUTH_ENABLED", "false").lower() == "true")
    auth_users_file: Path = field(default_factory=lambda: Path(os.getenv("AUTH_USERS_FILE", "secrets/auth_users.json")))
    auth_session_minutes: int = field(default_factory=lambda: int(os.getenv("AUTH_SESSION_MINUTES", "480")))
    auth_totp_issuer: str = field(default_factory=lambda: os.getenv("AUTH_TOTP_ISSUER", "Bybit Trading Bot"))
    auth_audit_log_file: str = field(default_factory=lambda: os.getenv("AUTH_AUDIT_LOG_FILE", "auth_audit.jsonl"))
    auth_cookie_secret: str = field(default_factory=lambda: os.getenv("AUTH_COOKIE_SECRET", ""))
    auth_cookie_name: str = field(default_factory=lambda: os.getenv("AUTH_COOKIE_NAME", "bybit_bot_session"))
    app_host: str = field(default_factory=lambda: os.getenv("APP_HOST", "0.0.0.0"))
    app_port: int = field(default_factory=lambda: int(os.getenv("APP_PORT", "8501")))
    dashboard_refresh_seconds: int = field(default_factory=lambda: int(os.getenv("DASHBOARD_REFRESH_SECONDS", "6")))
    control_refresh_seconds: int = field(default_factory=lambda: int(os.getenv("CONTROL_REFRESH_SECONDS", "10")))
    mobile_default_view: bool = field(default_factory=lambda: os.getenv("MOBILE_DEFAULT_VIEW", "true").lower() == "true")
    ema_fast: int = field(default_factory=lambda: int(os.getenv("EMA_FAST", "12")))
    ema_slow: int = field(default_factory=lambda: int(os.getenv("EMA_SLOW", "26")))
    rsi_period: int = field(default_factory=lambda: int(os.getenv("RSI_PERIOD", "14")))
    rsi_long_threshold: float = field(default_factory=lambda: float(os.getenv("RSI_LONG_THRESHOLD", "55")))
    rsi_short_threshold: float = field(default_factory=lambda: float(os.getenv("RSI_SHORT_THRESHOLD", "45")))
    macd_fast: int = field(default_factory=lambda: int(os.getenv("MACD_FAST", "12")))
    macd_slow: int = field(default_factory=lambda: int(os.getenv("MACD_SLOW", "26")))
    macd_signal: int = field(default_factory=lambda: int(os.getenv("MACD_SIGNAL", "9")))
    trend_ema: int = field(default_factory=lambda: int(os.getenv("TREND_EMA", "200")))
    atr_period: int = field(default_factory=lambda: int(os.getenv("ATR_PERIOD", "14")))
    atr_min_pct: float = field(default_factory=lambda: float(os.getenv("ATR_MIN_PCT", "0.003")))
    signal_score_threshold: int = field(default_factory=lambda: int(os.getenv("SIGNAL_SCORE_THRESHOLD", "3")))
    cooldown_bars: int = field(default_factory=lambda: int(os.getenv("COOLDOWN_BARS", "4")))
    fee_rate: float = field(default_factory=lambda: float(os.getenv("FEE_RATE", "0.0006")))
    invert_signals: bool = field(default_factory=lambda: os.getenv("INVERT_SIGNALS", "false").lower() == "true")
    filter_symbols_by_backtest: bool = field(
        default_factory=lambda: os.getenv("FILTER_SYMBOLS_BY_BACKTEST", "true").lower() == "true"
    )
    backtest_filter_candles: int = field(default_factory=lambda: int(os.getenv("BACKTEST_FILTER_CANDLES", "500")))
    min_backtest_profit_factor: float = field(default_factory=lambda: float(os.getenv("MIN_BACKTEST_PROFIT_FACTOR", "1.5")))
    max_backtest_drawdown_pct: float = field(default_factory=lambda: float(os.getenv("MAX_BACKTEST_DRAWDOWN_PCT", "20")))
    min_backtest_trades: int = field(default_factory=lambda: int(os.getenv("MIN_BACKTEST_TRADES", "10")))

    def ensure_directories(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @property
    def trade_log_path(self) -> Path:
        return self.log_dir / self.trade_log_file

    @property
    def event_log_path(self) -> Path:
        return self.log_dir / self.event_log_file

    @property
    def auth_audit_log_path(self) -> Path:
        return self.log_dir / self.auth_audit_log_file

    def validate(self) -> None:
        if not self.paper_trading and (not self.api_key or not self.api_secret):
            raise ValueError("BYBIT_API_KEY and BYBIT_API_SECRET are required when paper trading is disabled.")
        if self.risk_per_trade <= 0 or self.risk_per_trade > 0.05:
            raise ValueError("RISK_PER_TRADE must be between 0 and 0.05.")
        if self.stop_loss_pct <= 0 or self.take_profit_pct <= 0:
            raise ValueError("STOP_LOSS_PCT and TAKE_PROFIT_PCT must be positive.")
        if not self.symbols:
            raise ValueError("At least one symbol must be configured in TRADING_SYMBOLS.")
        if self.signal_score_threshold < 2 or self.signal_score_threshold > 4:
            raise ValueError("SIGNAL_SCORE_THRESHOLD must be between 2 and 4.")
        if self.cooldown_bars < 0:
            raise ValueError("COOLDOWN_BARS must be non-negative.")
        if self.fee_rate < 0 or self.fee_rate > 0.01:
            raise ValueError("FEE_RATE must be between 0 and 0.01.")
        if self.backtest_filter_candles < 100:
            raise ValueError("BACKTEST_FILTER_CANDLES must be at least 100.")
        if self.min_backtest_profit_factor < 0:
            raise ValueError("MIN_BACKTEST_PROFIT_FACTOR must be non-negative.")
        if self.max_backtest_drawdown_pct <= 0:
            raise ValueError("MAX_BACKTEST_DRAWDOWN_PCT must be positive.")
        if self.min_backtest_trades < 0:
            raise ValueError("MIN_BACKTEST_TRADES must be non-negative.")
        if self.auth_session_minutes <= 0:
            raise ValueError("AUTH_SESSION_MINUTES must be positive.")
        if self.auth_enabled and not self.auth_cookie_secret:
            raise ValueError("AUTH_COOKIE_SECRET is required when AUTH_ENABLED is true.")
        if self.app_port <= 0 or self.app_port > 65535:
            raise ValueError("APP_PORT must be between 1 and 65535.")
        if self.dashboard_refresh_seconds <= 0:
            raise ValueError("DASHBOARD_REFRESH_SECONDS must be positive.")
        if self.control_refresh_seconds <= 0:
            raise ValueError("CONTROL_REFRESH_SECONDS must be positive.")


def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    settings.validate()
    return settings
