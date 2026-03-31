from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from auth import AuthStore, AuthUser, has_permission, session_expiry
from backtest import Backtester
from config import get_settings
from trader import TraderEngine


@st.cache_resource
def get_engine() -> TraderEngine:
    return TraderEngine(get_settings())


@st.cache_resource
def get_backtester() -> Backtester:
    return Backtester(get_settings())


def _get_view_mode() -> str:
    raw_value = st.query_params.get("view", "dashboard")
    if isinstance(raw_value, list):
        raw_value = raw_value[0] if raw_value else "dashboard"
    return "control" if str(raw_value).lower() == "control" else "dashboard"


def _set_view_mode(view_mode: str) -> None:
    st.query_params["view"] = view_mode


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


@st.fragment(run_every="5s")
def render_mobile_control_snapshot(engine: TraderEngine) -> None:
    snapshot = engine.get_snapshot()
    status_tone = "green" if snapshot["status"] == "Running" else "red"
    col1, col2 = st.columns(2)
    with col1:
        _render_stat_card("Bot Status", snapshot["status"], status_tone)
    with col2:
        _render_stat_card("Total PnL", f"{snapshot['total_pnl']:.2f} USDT", _tone_from_pnl(snapshot["total_pnl"]))

    col3, col4 = st.columns(2)
    with col3:
        _render_stat_card("Open Positions", str(len(snapshot["open_positions"])))
    with col4:
        _render_stat_card("Trades", str(snapshot["trade_count"]))

    if snapshot["last_error"]:
        st.error(snapshot["last_error"])

    st.caption(f"Last update: {snapshot['last_update']}")


def _clear_auth_session() -> None:
    for key in ["auth_username", "auth_role", "auth_expires_at"]:
        st.session_state.pop(key, None)


def _get_authenticated_user(auth_store: AuthStore) -> AuthUser | None:
    username = st.session_state.get("auth_username", "")
    expires_at = st.session_state.get("auth_expires_at", "")
    if not username or not expires_at:
        return None
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        _clear_auth_session()
        return None
    if expiry <= datetime.now(timezone.utc):
        auth_store.log_event("session_expired", success=True, username=username)
        _clear_auth_session()
        return None
    user = auth_store.get_user(username)
    if not user or user.disabled:
        _clear_auth_session()
        return None
    st.session_state["auth_expires_at"] = session_expiry(auth_store.settings).isoformat()
    return user


def _render_bootstrap(auth_store: AuthStore) -> None:
    st.warning("Authentication is enabled, but no users exist yet. Create the first admin before you deploy publicly.")
    with st.form("bootstrap_admin_form", clear_on_submit=False):
        username = st.text_input("Admin username")
        password = st.text_input("Admin password", type="password")
        confirm_password = st.text_input("Confirm password", type="password")
        submitted = st.form_submit_button("Create Admin")
    if not submitted:
        return
    if password != confirm_password:
        st.error("Passwords do not match.")
        return
    try:
        user = auth_store.create_bootstrap_admin(username, password)
    except ValueError as exc:
        st.error(str(exc))
        return
    auth_store.log_event("bootstrap_admin_created", success=True, username=user.username)
    st.success("Admin user created. Scan the QR code in Google Authenticator, then log in.")
    st.image(auth_store.qr_image(user), caption="Google Authenticator setup QR", width=260)
    st.code(auth_store.provisioning_uri(user), language="text")
    st.info(f"Backup secret: {user.totp_secret}")


def _render_login(auth_store: AuthStore) -> AuthUser | None:
    st.subheader("Secure Login")
    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        totp_code = st.text_input("Google Authenticator code", max_chars=6)
        submitted = st.form_submit_button("Log In")
    if not submitted:
        return None
    user = auth_store.verify_login(username, password, totp_code)
    if not user:
        auth_store.log_event("login_failed", success=False, username=username.strip())
        st.error("Invalid username, password, or Google Authenticator code.")
        return None
    st.session_state["auth_username"] = user.username
    st.session_state["auth_role"] = user.role
    st.session_state["auth_expires_at"] = session_expiry(auth_store.settings).isoformat()
    auth_store.log_event("login_success", success=True, username=user.username)
    st.rerun()
    return user


def _require_permission(auth_store: AuthStore, user: AuthUser, permission: str, action_label: str) -> bool:
    if has_permission(user, permission):
        return True
    auth_store.log_event(
        "permission_denied",
        success=False,
        username=user.username,
        details={"permission": permission, "action": action_label},
    )
    st.error(f"You do not have permission to {action_label.lower()}.")
    return False


def render_dashboard() -> None:
    st.set_page_config(page_title="Bybit Trading Bot", layout="wide")
    view_mode = _get_view_mode()
    st.title("Bybit Trading Bot Dashboard" if view_mode == "dashboard" else "Bybit Bot Control")

    settings = get_settings()
    auth_store = AuthStore(settings)

    if settings.auth_enabled:
        if not auth_store.has_users():
            _render_bootstrap(auth_store)
            return
        user = _get_authenticated_user(auth_store)
        if not user:
            _render_login(auth_store)
            return
        if not _require_permission(auth_store, user, "view_dashboard", "View Dashboard"):
            return
    else:
        user = None

    engine = get_engine()

    st.sidebar.header("Controls")
    selected_view = st.sidebar.radio(
        "View",
        options=["control", "dashboard"],
        format_func=lambda item: "Control" if item == "control" else "Dashboard",
        index=0 if view_mode == "control" else 1,
    )
    if selected_view != view_mode:
        _set_view_mode(selected_view)
        st.rerun()
    st.sidebar.write(f"Mode: `{'Paper' if settings.paper_trading else 'Live'}`")
    st.sidebar.write(f"Category: `{settings.category}`")
    st.sidebar.write(f"Symbols: `{', '.join(settings.symbols)}`")
    st.sidebar.write(f"Backtest Filter: `{'On' if settings.filter_symbols_by_backtest else 'Off'}`")
    if user:
        st.sidebar.write(f"User: `{user.username}`")
        st.sidebar.write(f"Role: `{user.role}`")
    if st.sidebar.button("Start Bot", width="stretch", disabled=bool(user) and not has_permission(user, "start_bot")):
        if not user or _require_permission(auth_store, user, "start_bot", "Start Bot"):
            engine.start()
            if user:
                auth_store.log_event("bot_start", success=engine.status == "Running", username=user.username)
    if st.sidebar.button("Stop Bot", width="stretch", disabled=bool(user) and not has_permission(user, "stop_bot")):
        if not user or _require_permission(auth_store, user, "stop_bot", "Stop Bot"):
            engine.stop()
            if user:
                auth_store.log_event("bot_stop", success=True, username=user.username)
    if st.sidebar.button("Refresh", width="stretch"):
        st.rerun()
    if user and st.sidebar.button("Log Out", width="stretch"):
        auth_store.log_event("logout", success=True, username=user.username)
        _clear_auth_session()
        st.rerun()
    st.sidebar.caption("Edit `.env` to change trading parameters, then restart Streamlit.")
    if view_mode == "control":
        render_mobile_control_snapshot(engine)
        st.info("Tip: bookmark this page on your phone with `?view=control` for a faster remote control screen.")
    else:
        render_live_snapshot(engine)

        st.subheader("Quick Backtest")
        selected_symbol = st.selectbox("Symbol", settings.symbols)
        can_run_backtest = not user or has_permission(user, "run_backtest")
        if user and not can_run_backtest:
            st.caption("Your role does not include backtest execution.")
        if st.button("Run Backtest", disabled=not can_run_backtest):
            if user and not _require_permission(auth_store, user, "run_backtest", "Run Backtest"):
                return
            backtester = get_backtester()
            result = backtester.run(selected_symbol)
            if user:
                auth_store.log_event(
                    "backtest_run",
                    success=True,
                    username=user.username,
                    details={"symbol": selected_symbol},
                )
            bt1, bt2, bt3, bt4 = st.columns(4)
            bt1.metric("Return", f"{result.total_return_pct:.2f}%")
            bt2.metric("Trades", result.trades)
            bt3.metric("Win Rate", f"{result.win_rate_pct:.2f}%")
            bt4.metric("Final Equity", f"{result.final_equity:.2f} USDT")
            bt5, bt6, bt7 = st.columns(3)
            bt5.metric("Max Drawdown", f"{result.max_drawdown_pct:.2f}%")
            bt6.metric("Profit Factor", f"{result.profit_factor:.2f}")
            bt7.metric("Avg Trade", f"{result.avg_trade_pct:.2f}%")

        if user:
            with st.expander("Google Authenticator Setup"):
                st.write("Keep this QR code private. It can generate valid login codes for your account.")
                st.image(auth_store.qr_image(user), caption="Google Authenticator QR", width=220)
                st.code(auth_store.provisioning_uri(user), language="text")
