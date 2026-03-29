from __future__ import annotations

import pandas as pd
import streamlit as st

from backtest import Backtester
from config import get_settings
from trader import TraderEngine


@st.cache_resource
def get_engine() -> TraderEngine:
    return TraderEngine(get_settings())


@st.cache_resource
def get_backtester() -> Backtester:
    return Backtester(get_settings())


def _render_stat_card(label: str, value: str, tone: str = "neutral") -> None:
    tones = {
        "green": {"bg": "#000000", "border": "#22c55e", "text": "#4ade80"},
        "red": {"bg": "#000000", "border": "#ef4444", "text": "#f87171"},
        "neutral": {"bg": "#000000", "border": "#6b7280", "text": "#f9fafb"},
    }
    palette = tones.get(tone, tones["neutral"])
    st.markdown(
        f"""
        <div style="padding:14px 16px;border-radius:14px;border:1px solid {palette['border']};
                    background:{palette['bg']};margin-bottom:8px;">
            <div style="font-size:0.85rem;color:#9ca3af;">{label}</div>
            <div style="font-size:1.5rem;font-weight:700;color:{palette['text']};">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _tone_from_pnl(value: float) -> str:
    if value > 0:
        return "green"
    if value < 0:
        return "red"
    return "neutral"


def _style_trades(frame: pd.DataFrame):
    def color_pnl(value: float) -> str:
        if value > 0:
            return "color: #146c2e; font-weight: 700;"
        if value < 0:
            return "color: #b42318; font-weight: 700;"
        return "color: #475467;"

    def color_side(value: str) -> str:
        if value == "long":
            return "color: #146c2e; font-weight: 700;"
        if value == "short":
            return "color: #b42318; font-weight: 700;"
        return ""

    styled = frame.style
    if "pnl" in frame.columns:
        styled = styled.map(color_pnl, subset=["pnl"])
    if "side" in frame.columns:
        styled = styled.map(color_side, subset=["side"])
    return styled


@st.fragment(run_every="3s")
def render_live_snapshot(engine: TraderEngine) -> None:
    snapshot = engine.get_snapshot()

    row1 = st.columns(5)
    with row1[0]:
        _render_stat_card("Bot Status", snapshot["status"], "green" if snapshot["status"] == "Running" else "red")
    with row1[1]:
        _render_stat_card("Trades Executed", str(snapshot["trade_count"]))
    with row1[2]:
        _render_stat_card("Balance", f"{snapshot['balance']:.2f} USDT")
    with row1[3]:
        _render_stat_card("Total PnL", f"{snapshot['total_pnl']:.2f} USDT", _tone_from_pnl(snapshot["total_pnl"]))
    with row1[4]:
        _render_stat_card("Open Positions", str(len(snapshot["open_positions"])))

    if snapshot["last_error"]:
        st.error(snapshot["last_error"])

    row2 = st.columns(2)
    with row2[0]:
        _render_stat_card("Realized PnL", f"{snapshot['realized_pnl']:.2f} USDT", _tone_from_pnl(snapshot["realized_pnl"]))
    with row2[1]:
        _render_stat_card(
            "Unrealized PnL",
            f"{snapshot['unrealized_pnl']:.2f} USDT",
            _tone_from_pnl(snapshot["unrealized_pnl"]),
        )

    st.subheader("Open Positions")
    positions = pd.DataFrame(snapshot["open_positions"])
    if positions.empty:
        st.info("No open positions.")
    else:
        st.dataframe(positions, width="stretch", hide_index=True)

    st.subheader("Recent Trades")
    trades = pd.DataFrame(snapshot["recent_trades"])
    if trades.empty:
        st.info("No trades recorded yet.")
    else:
        st.dataframe(_style_trades(trades), width="stretch", hide_index=True)

    st.subheader("Symbol Filter")
    filter_results = pd.DataFrame(snapshot["symbol_filter_results"])
    if filter_results.empty:
        st.info("No symbol filter results yet. Start the bot to run the startup filter.")
    else:
        st.dataframe(filter_results, width="stretch", hide_index=True)

    st.caption(f"Last update: {snapshot['last_update']}")


def render_dashboard() -> None:
    st.set_page_config(page_title="Bybit Trading Bot", layout="wide")
    st.title("Bybit Trading Bot Dashboard")

    settings = get_settings()
    engine = get_engine()
    backtester = get_backtester()

    st.sidebar.header("Controls")
    st.sidebar.write(f"Mode: `{'Paper' if settings.paper_trading else 'Live'}`")
    st.sidebar.write(f"Category: `{settings.category}`")
    st.sidebar.write(f"Symbols: `{', '.join(settings.symbols)}`")
    st.sidebar.write(f"Backtest Filter: `{'On' if settings.filter_symbols_by_backtest else 'Off'}`")
    if st.sidebar.button("Start Bot", width="stretch"):
        engine.start()
    if st.sidebar.button("Stop Bot", width="stretch"):
        engine.stop()
    if st.sidebar.button("Refresh", width="stretch"):
        st.rerun()
    st.sidebar.caption("Edit `.env` to change trading parameters, then restart Streamlit.")
    render_live_snapshot(engine)

    st.subheader("Quick Backtest")
    selected_symbol = st.selectbox("Symbol", settings.symbols)
    if st.button("Run Backtest"):
        result = backtester.run(selected_symbol)
        bt1, bt2, bt3, bt4 = st.columns(4)
        bt1.metric("Return", f"{result.total_return_pct:.2f}%")
        bt2.metric("Trades", result.trades)
        bt3.metric("Win Rate", f"{result.win_rate_pct:.2f}%")
        bt4.metric("Final Equity", f"{result.final_equity:.2f} USDT")
        bt5, bt6, bt7 = st.columns(3)
        bt5.metric("Max Drawdown", f"{result.max_drawdown_pct:.2f}%")
        bt6.metric("Profit Factor", f"{result.profit_factor:.2f}")
        bt7.metric("Avg Trade", f"{result.avg_trade_pct:.2f}%")
