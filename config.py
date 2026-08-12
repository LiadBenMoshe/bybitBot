from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()


def _split_symbols(raw: str) -> List[str]:
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def _float_env(primary: str, default: str, fallback: str = "") -> float:
    """Read a float env var, optionally falling back to a deprecated key."""
    raw = os.getenv(primary)
    if raw is None and fallback:
        raw = os.getenv(fallback)
    return float(raw if raw is not None else default)


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
    max_position_notional_pct: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_NOTIONAL_PCT", "0.75")))
    break_even_trigger_pct: float = field(default_factory=lambda: float(os.getenv("BREAK_EVEN_TRIGGER_PCT", "0.003")))
    break_even_offset_pct: float = field(default_factory=lambda: float(os.getenv("BREAK_EVEN_OFFSET_PCT", "0.0003")))
    max_consecutive_losses: int = field(default_factory=lambda: int(os.getenv("MAX_CONSECUTIVE_LOSSES", "2")))
    cooldown_after_loss_bars: int = field(default_factory=lambda: int(os.getenv("COOLDOWN_AFTER_LOSS_BARS", "3")))
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
    ema_fast: int = field(default_factory=lambda: int(os.getenv("EMA_FAST", "9")))
    ema_slow: int = field(default_factory=lambda: int(os.getenv("EMA_SLOW", "21")))
    rsi_period: int = field(default_factory=lambda: int(os.getenv("RSI_PERIOD", "14")))
    rsi_long_threshold: float = field(default_factory=lambda: float(os.getenv("RSI_LONG_THRESHOLD", "52")))
    rsi_short_threshold: float = field(default_factory=lambda: float(os.getenv("RSI_SHORT_THRESHOLD", "48")))
    macd_fast: int = field(default_factory=lambda: int(os.getenv("MACD_FAST", "12")))
    macd_slow: int = field(default_factory=lambda: int(os.getenv("MACD_SLOW", "26")))
    macd_signal: int = field(default_factory=lambda: int(os.getenv("MACD_SIGNAL", "9")))
    trend_ema: int = field(default_factory=lambda: int(os.getenv("TREND_EMA", "50")))
    atr_period: int = field(default_factory=lambda: int(os.getenv("ATR_PERIOD", "14")))
    atr_min_pct: float = field(default_factory=lambda: float(os.getenv("ATR_MIN_PCT", "0.002")))
    adx_period: int = field(default_factory=lambda: int(os.getenv("ADX_PERIOD", "14")))
    min_adx: float = field(default_factory=lambda: float(os.getenv("MIN_ADX", "20")))
    volume_ma_period: int = field(default_factory=lambda: int(os.getenv("VOLUME_MA_PERIOD", "20")))
    min_volume_ratio: float = field(default_factory=lambda: float(os.getenv("MIN_VOLUME_RATIO", "1.1")))
    breakout_lookback: int = field(default_factory=lambda: int(os.getenv("BREAKOUT_LOOKBACK", "12")))
    min_trend_strength_pct: float = field(default_factory=lambda: float(os.getenv("MIN_TREND_STRENGTH_PCT", "0.001")))
    signal_score_threshold: int = field(default_factory=lambda: int(os.getenv("SIGNAL_SCORE_THRESHOLD", "5")))
    extreme_entry_mode: bool = field(default_factory=lambda: os.getenv("EXTREME_ENTRY_MODE", "true").lower() == "true")
    min_expected_move_pct: float = field(default_factory=lambda: float(os.getenv("MIN_EXPECTED_MOVE_PCT", "0.008")))
    min_signal_confidence: float = field(default_factory=lambda: float(os.getenv("MIN_SIGNAL_CONFIDENCE", "0.65")))
    require_breakout_confirmation: bool = field(
        default_factory=lambda: os.getenv("REQUIRE_BREAKOUT_CONFIRMATION", "true").lower() == "true"
    )
    pullback_lookback: int = field(default_factory=lambda: int(os.getenv("PULLBACK_LOOKBACK", "4")))
    breakout_buffer_pct: float = field(default_factory=lambda: float(os.getenv("BREAKOUT_BUFFER_PCT", "0.0008")))
    atr_stop_multiple: float = field(default_factory=lambda: float(os.getenv("ATR_STOP_MULTIPLE", "1.2")))
    atr_target_multiple: float = field(default_factory=lambda: float(os.getenv("ATR_TARGET_MULTIPLE", "2.2")))
    max_rsi_long: float = field(default_factory=lambda: float(os.getenv("MAX_RSI_LONG", "72")))
    min_rsi_short: float = field(default_factory=lambda: float(os.getenv("MIN_RSI_SHORT", "28")))
    min_body_to_range_ratio: float = field(default_factory=lambda: float(os.getenv("MIN_BODY_TO_RANGE_RATIO", "0.45")))
    min_range_width_pct: float = field(default_factory=lambda: float(os.getenv("MIN_RANGE_WIDTH_PCT", "0.004")))
    ema_retest_tolerance_pct: float = field(default_factory=lambda: float(os.getenv("EMA_RETEST_TOLERANCE_PCT", "0.0025")))
    require_macd_hist_improving: bool = field(
        default_factory=lambda: os.getenv("REQUIRE_MACD_HIST_IMPROVING", "true").lower() == "true"
    )
    cooldown_bars: int = field(default_factory=lambda: int(os.getenv("COOLDOWN_BARS", "2")))
    fee_rate: float = field(default_factory=lambda: float(os.getenv("FEE_RATE", "0.0006")))
    invert_signals: bool = field(default_factory=lambda: os.getenv("INVERT_SIGNALS", "false").lower() == "true")
    filter_symbols_by_backtest: bool = field(
        default_factory=lambda: os.getenv("FILTER_SYMBOLS_BY_BACKTEST", "true").lower() == "true"
    )
    backtest_filter_candles: int = field(default_factory=lambda: int(os.getenv("BACKTEST_FILTER_CANDLES", "500")))
    min_backtest_profit_factor: float = field(default_factory=lambda: float(os.getenv("MIN_BACKTEST_PROFIT_FACTOR", "1.5")))
    max_backtest_drawdown_pct: float = field(default_factory=lambda: float(os.getenv("MAX_BACKTEST_DRAWDOWN_PCT", "20")))
    min_backtest_trades: int = field(default_factory=lambda: int(os.getenv("MIN_BACKTEST_TRADES", "10")))
    min_backtest_expectancy_r: float = field(default_factory=lambda: float(os.getenv("MIN_BACKTEST_EXPECTANCY_R", "0.05")))
    require_backtest_approval: bool = field(
        default_factory=lambda: os.getenv("REQUIRE_BACKTEST_APPROVAL", "true").lower() == "true"
    )
    # --- transaction costs -------------------------------------------------
    maker_fee_rate: float = field(default_factory=lambda: _float_env("MAKER_FEE_RATE", "0.0002"))
    taker_fee_rate: float = field(default_factory=lambda: _float_env("TAKER_FEE_RATE", "0.00055", fallback="FEE_RATE"))
    entry_slippage_pct: float = field(default_factory=lambda: _float_env("ENTRY_SLIPPAGE_PCT", "0.0002"))
    exit_slippage_pct: float = field(default_factory=lambda: _float_env("EXIT_SLIPPAGE_PCT", "0.0004"))
    auto_sync_fee_rates: bool = field(default_factory=lambda: os.getenv("AUTO_SYNC_FEE_RATES", "true").lower() == "true")
    # --- maker (post-only) entries ----------------------------------------
    maker_entry_enabled: bool = field(default_factory=lambda: os.getenv("MAKER_ENTRY_ENABLED", "true").lower() == "true")
    maker_entry_timeout_bars: int = field(default_factory=lambda: int(os.getenv("MAKER_ENTRY_TIMEOUT_BARS", "1")))
    maker_max_chase_pct: float = field(default_factory=lambda: float(os.getenv("MAKER_MAX_CHASE_PCT", "0.0015")))
    maker_entry_fallback: str = field(default_factory=lambda: os.getenv("MAKER_ENTRY_FALLBACK", "none").strip().lower())
    # --- cost-aware entry gate --------------------------------------------
    min_target_to_cost_ratio: float = field(default_factory=lambda: float(os.getenv("MIN_TARGET_TO_COST_RATIO", "4.0")))
    min_net_reward_risk: float = field(default_factory=lambda: float(os.getenv("MIN_NET_REWARD_RISK", "1.5")))
    require_positive_expectancy: bool = field(
        default_factory=lambda: os.getenv("REQUIRE_POSITIVE_EXPECTANCY", "true").lower() == "true"
    )
    # --- exits --------------------------------------------------------------
    take_profit_mode: str = field(default_factory=lambda: os.getenv("TAKE_PROFIT_MODE", "fixed").strip().lower())
    trail_atr_multiple: float = field(default_factory=lambda: float(os.getenv("TRAIL_ATR_MULTIPLE", "1.5")))

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
        if self.max_position_notional_pct <= 0 or self.max_position_notional_pct > 5:
            raise ValueError("MAX_POSITION_NOTIONAL_PCT must be between 0 and 5.")
        if self.break_even_trigger_pct < 0 or self.break_even_offset_pct < 0:
            raise ValueError("BREAK_EVEN_TRIGGER_PCT and BREAK_EVEN_OFFSET_PCT must be non-negative.")
        if self.max_consecutive_losses < 0:
            raise ValueError("MAX_CONSECUTIVE_LOSSES must be non-negative.")
        if self.leverage < 1 or self.leverage > 100:
            raise ValueError("DEFAULT_LEVERAGE must be between 1 and 100.")
        if self.max_positions < 1:
            raise ValueError("MAX_OPEN_POSITIONS must be at least 1.")
        if self.initial_balance <= 0:
            raise ValueError("INITIAL_BALANCE must be positive.")
        if self.cooldown_after_loss_bars < 0:
            raise ValueError("COOLDOWN_AFTER_LOSS_BARS must be non-negative.")
        if not self.symbols:
            raise ValueError("At least one symbol must be configured in TRADING_SYMBOLS.")
        if self.timeframe not in {"5", "15", "30", "60", "240", "D"}:
            raise ValueError("TIMEFRAME must be one of 5, 15, 30, 60, 240, or D.")
        if self.signal_score_threshold < 3 or self.signal_score_threshold > 8:
            raise ValueError("SIGNAL_SCORE_THRESHOLD must be between 3 and 8.")
        if self.adx_period < 2:
            raise ValueError("ADX_PERIOD must be at least 2.")
        if self.min_adx < 0:
            raise ValueError("MIN_ADX must be non-negative.")
        if self.volume_ma_period < 2:
            raise ValueError("VOLUME_MA_PERIOD must be at least 2.")
        if self.min_volume_ratio <= 0:
            raise ValueError("MIN_VOLUME_RATIO must be positive.")
        if self.breakout_lookback < 2:
            raise ValueError("BREAKOUT_LOOKBACK must be at least 2.")
        if self.min_trend_strength_pct < 0:
            raise ValueError("MIN_TREND_STRENGTH_PCT must be non-negative.")
        if self.min_expected_move_pct < 0:
            raise ValueError("MIN_EXPECTED_MOVE_PCT must be non-negative.")
        if self.min_signal_confidence < 0 or self.min_signal_confidence > 1:
            raise ValueError("MIN_SIGNAL_CONFIDENCE must be between 0 and 1.")
        if self.pullback_lookback < 1:
            raise ValueError("PULLBACK_LOOKBACK must be at least 1.")
        if self.breakout_buffer_pct < 0:
            raise ValueError("BREAKOUT_BUFFER_PCT must be non-negative.")
        if self.atr_stop_multiple <= 0 or self.atr_target_multiple <= 0:
            raise ValueError("ATR_STOP_MULTIPLE and ATR_TARGET_MULTIPLE must be positive.")
        if self.max_rsi_long <= self.rsi_long_threshold or self.max_rsi_long > 100:
            raise ValueError("MAX_RSI_LONG must be greater than RSI_LONG_THRESHOLD and at most 100.")
        if self.min_rsi_short < 0 or self.min_rsi_short >= self.rsi_short_threshold:
            raise ValueError("MIN_RSI_SHORT must be non-negative and below RSI_SHORT_THRESHOLD.")
        if self.min_body_to_range_ratio <= 0 or self.min_body_to_range_ratio > 1:
            raise ValueError("MIN_BODY_TO_RANGE_RATIO must be between 0 and 1.")
        if self.min_range_width_pct < 0:
            raise ValueError("MIN_RANGE_WIDTH_PCT must be non-negative.")
        if self.ema_retest_tolerance_pct < 0:
            raise ValueError("EMA_RETEST_TOLERANCE_PCT must be non-negative.")
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
        for name, value in (
            ("MAKER_FEE_RATE", self.maker_fee_rate),
            ("TAKER_FEE_RATE", self.taker_fee_rate),
            ("ENTRY_SLIPPAGE_PCT", self.entry_slippage_pct),
            ("EXIT_SLIPPAGE_PCT", self.exit_slippage_pct),
        ):
            if value < 0 or value > 0.01:
                raise ValueError(f"{name} must be between 0 and 0.01.")
        if self.maker_entry_timeout_bars < 0:
            raise ValueError("MAKER_ENTRY_TIMEOUT_BARS must be non-negative.")
        if self.maker_max_chase_pct < 0:
            raise ValueError("MAKER_MAX_CHASE_PCT must be non-negative.")
        if self.maker_entry_fallback not in {"none", "market"}:
            raise ValueError("MAKER_ENTRY_FALLBACK must be either none or market.")
        if self.min_target_to_cost_ratio < 1:
            raise ValueError("MIN_TARGET_TO_COST_RATIO must be at least 1.")
        if self.min_net_reward_risk < 0.5:
            raise ValueError("MIN_NET_REWARD_RISK must be at least 0.5.")
        if self.take_profit_mode not in {"fixed", "trail"}:
            raise ValueError("TAKE_PROFIT_MODE must be either fixed or trail.")
        if self.trail_atr_multiple <= 0:
            raise ValueError("TRAIL_ATR_MULTIPLE must be positive.")
        if self.atr_target_multiple <= self.atr_stop_multiple:
            raise ValueError("ATR_TARGET_MULTIPLE must be greater than ATR_STOP_MULTIPLE.")
        if self.min_backtest_expectancy_r < 0:
            raise ValueError("MIN_BACKTEST_EXPECTANCY_R must be non-negative.")
        round_trip = self.round_trip_cost_pct()
        if self.break_even_trigger_pct > 0 and self.break_even_trigger_pct <= round_trip * 2:
            raise ValueError(
                f"BREAK_EVEN_TRIGGER_PCT must exceed twice the round-trip cost ({round_trip * 2:.5f}); "
                "arming break-even inside the cost band locks in a guaranteed net loss."
            )

    def round_trip_cost_pct(self) -> float:
        """Total cost of opening and closing one position, as a fraction of notional."""
        from costs import build_cost_model

        return build_cost_model(self).round_trip_pct()

    def sizing_warnings(self) -> List[str]:
        """Non-fatal configuration problems, logged once the logger is configured."""
        warnings: List[str] = []
        min_stop_distance = self.atr_stop_multiple * self.atr_min_pct
        if min_stop_distance > 0:
            wanted_notional = self.risk_per_trade / min_stop_distance
            if wanted_notional > self.max_position_notional_pct:
                warnings.append(
                    f"RISK_PER_TRADE={self.risk_per_trade} implies {wanted_notional:.2f}x equity notional at the "
                    f"minimum stop distance, but MAX_POSITION_NOTIONAL_PCT={self.max_position_notional_pct} caps it. "
                    "The cap will bind on most trades and risk-per-trade will not govern sizing. "
                    f"Lower RISK_PER_TRADE to about {self.max_position_notional_pct * min_stop_distance:.4f} "
                    "or raise the notional cap."
                )
        from costs import build_cost_model

        cost = build_cost_model(self)
        derived_floor = max(
            cost.min_atr_pct_for_target(self.atr_target_multiple, self.min_target_to_cost_ratio),
            cost.min_atr_pct_for_reward_risk(
                self.atr_target_multiple, self.atr_stop_multiple, self.min_net_reward_risk
            ),
        )
        if derived_floor > self.atr_min_pct:
            warnings.append(
                f"Cost model implies a volatility floor of {derived_floor:.5f} ATR%, above ATR_MIN_PCT="
                f"{self.atr_min_pct}. The derived floor governs; low-volatility setups will be rejected."
            )
        return warnings


@dataclass(frozen=True, slots=True)
class EditableField:
    """One setting the web UI is allowed to write back into .env."""

    env_key: str
    attr: str
    kind: str  # symbols | choice | int | float | bool
    label: str
    help: str
    group: str
    choices: Tuple[str, ...] = ()
    step: str = ""


TIMEFRAME_CHOICES: Tuple[str, ...] = ("5", "15", "30", "60", "240", "D")

# The single source of truth for the Settings tab. Adding a field here is all it
# takes for it to render, validate and persist - as long as validate() has a rule
# for it, which is where the error message the UI shows comes from.
UI_EDITABLE_FIELDS: Tuple[EditableField, ...] = (
    EditableField(
        "TRADING_SYMBOLS", "symbols", "symbols", "Trading symbols",
        "Comma separated, e.g. BTCUSDT,ETHUSDT.", "Market",
    ),
    EditableField(
        "TIMEFRAME", "timeframe", "choice", "Timeframe",
        "Candle interval in minutes (D = daily).", "Market", choices=TIMEFRAME_CHOICES,
    ),
    EditableField(
        "RISK_PER_TRADE", "risk_per_trade", "float", "Risk per trade",
        "Fraction of equity risked per position. Max 0.05.", "Risk", step="0.001",
    ),
    EditableField(
        "DEFAULT_LEVERAGE", "leverage", "int", "Leverage",
        "Applied to every symbol at startup. 1-100.", "Risk",
    ),
    EditableField(
        "MAX_OPEN_POSITIONS", "max_positions", "int", "Max open positions",
        "How many symbols can hold a position at once.", "Risk",
    ),
    EditableField(
        "MAX_CONSECUTIVE_LOSSES", "max_consecutive_losses", "int", "Max consecutive losses",
        "New entries pause after this many losers. 0 disables the pause.", "Risk",
    ),
    EditableField(
        "INITIAL_BALANCE", "initial_balance", "float", "Initial balance",
        "Paper-mode starting equity only. In live mode the balance comes from Bybit.",
        "Account", step="0.01",
    ),
    EditableField(
        "INVERT_SIGNALS", "invert_signals", "bool", "Invert signals",
        "Flips long/short inside the strategy, before exits are priced.", "Behaviour",
    ),
)

EDITABLE_BY_ENV: Dict[str, EditableField] = {item.env_key: item for item in UI_EDITABLE_FIELDS}

# Serialises the temporary os.environ mutation below against _reload_runtime_settings,
# since both run on request threads and both touch process-global state.
ENV_MUTATION_LOCK = threading.Lock()


def build_candidate_settings(overrides: Dict[str, str]) -> Settings:
    """Validate `overrides` as if they were already in the environment.

    Runs the full existing validate() rule set - including the cross-field rules -
    without writing to .env, so a rejected value never reaches the file.
    Deliberately does not call ensure_directories(): this must have no side effects.
    """
    with ENV_MUTATION_LOCK:
        previous: Dict[str, Optional[str]] = {key: os.environ.get(key) for key in overrides}
        try:
            os.environ.update(overrides)
            candidate = Settings()
            candidate.validate()
            return candidate
        finally:
            for key, old in previous.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old


def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    settings.validate()
    return settings
