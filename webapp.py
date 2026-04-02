from __future__ import annotations

import base64
from io import BytesIO
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from auth import AuthStore, AuthUser, create_session_token, has_permission, session_expiry, verify_session_token
from backtest import Backtester
from config import Settings, get_settings
from logger import configure_logging
from trader import TraderEngine

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
SETTINGS = get_settings()
configure_logging(SETTINGS.event_log_path)


@dataclass(slots=True)
class SessionContext:
    user: Optional[AuthUser]
    expires_at: Optional[datetime]


@lru_cache(maxsize=1)
def get_engine() -> TraderEngine:
    return TraderEngine(SETTINGS)


@lru_cache(maxsize=1)
def get_backtester() -> Backtester:
    return Backtester(SETTINGS)


@lru_cache(maxsize=1)
def get_auth_store() -> AuthStore:
    return AuthStore(SETTINGS)


app = FastAPI(title="Bybit Trading Bot")
ENV_PATH = BASE_DIR / ".env"


def _default_view_path() -> str:
    return "/control" if SETTINGS.mobile_default_view else "/dashboard"


def _set_session_cookie(response: RedirectResponse | HTMLResponse | JSONResponse, user: AuthUser, expires_at: datetime) -> None:
    token = create_session_token(SETTINGS, user, expires_at)
    max_age = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(
        key=SETTINGS.auth_cookie_name,
        value=token,
        max_age=max_age,
        expires=max_age,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def _clear_session_cookie(response: RedirectResponse | HTMLResponse | JSONResponse) -> None:
    response.delete_cookie(key=SETTINGS.auth_cookie_name, path="/")


def _read_session(request: Request, auth_store: AuthStore) -> SessionContext:
    if not SETTINGS.auth_enabled:
        return SessionContext(user=None, expires_at=None)
    token = request.cookies.get(SETTINGS.auth_cookie_name)
    if not token:
        return SessionContext(user=None, expires_at=None)
    payload = verify_session_token(SETTINGS, token)
    if not payload:
        return SessionContext(user=None, expires_at=None)
    try:
        expires_at = datetime.fromisoformat(payload["expires_at"])
    except ValueError:
        return SessionContext(user=None, expires_at=None)
    if expires_at <= datetime.now(timezone.utc):
        auth_store.log_event("session_expired", success=True, username=payload["username"])
        return SessionContext(user=None, expires_at=None)
    user = auth_store.get_user(payload["username"])
    if not user or user.disabled:
        return SessionContext(user=None, expires_at=None)
    return SessionContext(user=user, expires_at=expires_at)


def _require_user(request: Request, auth_store: AuthStore) -> SessionContext:
    session = _read_session(request, auth_store)
    if SETTINGS.auth_enabled and not session.user:
        raise PermissionError("Authentication required.")
    return session


def _refresh_session(response: RedirectResponse | HTMLResponse | JSONResponse, session: SessionContext) -> None:
    if SETTINGS.auth_enabled and session.user:
        expires_at = session_expiry(SETTINGS)
        _set_session_cookie(response, session.user, expires_at)


def _render_page(request: Request, view_mode: str, session: SessionContext) -> HTMLResponse:
    response = TEMPLATES.TemplateResponse(
        request=request,
        name="app.html",
        context={
            "request": request,
            "user": session.user,
            "settings": SETTINGS,
            "view_mode": view_mode,
        },
    )
    _refresh_session(response, session)
    return response


def _redirect_to_login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


def _redirect_to_default() -> RedirectResponse:
    return RedirectResponse(url=_default_view_path(), status_code=status.HTTP_303_SEE_OTHER)


def _permission_denied(auth_store: AuthStore, user: Optional[AuthUser], permission: str, action_label: str) -> JSONResponse:
    auth_store.log_event(
        "permission_denied",
        success=False,
        username=user.username if user else "",
        details={"permission": permission, "action": action_label},
    )
    return JSONResponse({"error": f"You do not have permission to {action_label.lower()}."}, status_code=403)


def _json_response(payload, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=jsonable_encoder(payload), status_code=status_code)


def _reload_runtime_settings() -> None:
    load_dotenv(ENV_PATH, override=True)
    fresh_settings = get_settings()
    for field in fields(Settings):
        setattr(SETTINGS, field.name, getattr(fresh_settings, field.name))
    get_engine.cache_clear()
    get_backtester.cache_clear()
    get_auth_store.cache_clear()


def _update_env_values(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key, _, value = line.partition("=")
        env_key = key.strip()
        if env_key in updates:
            new_lines.append(f"{env_key}={updates[env_key]}")
            seen.add(env_key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


@app.get("/", response_class=HTMLResponse, response_model=None)
def root():
    return _redirect_to_default()


@app.get("/login", response_class=HTMLResponse, response_model=None)
def login_page(request: Request):
    auth_store = get_auth_store()
    if not SETTINGS.auth_enabled:
        return _redirect_to_default()
    session = _read_session(request, auth_store)
    if session.user:
        response = _redirect_to_default()
        _refresh_session(response, session)
        return response
    if not auth_store.has_users():
        response = TEMPLATES.TemplateResponse(
            request=request,
            name="login.html",
            context={"request": request, "bootstrap_required": True, "error": "", "bootstrap_data": None},
        )
        return response
    return TEMPLATES.TemplateResponse(
        request=request,
        name="login.html",
        context={"request": request, "bootstrap_required": False, "error": "", "bootstrap_data": None},
    )


@app.post("/login", response_class=HTMLResponse, response_model=None)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    totp_code: str = Form(...),
):
    auth_store = get_auth_store()
    if not SETTINGS.auth_enabled:
        return _redirect_to_default()
    user = auth_store.verify_login(username, password, totp_code)
    if not user:
        auth_store.log_event("login_failed", success=False, username=username.strip())
        return TEMPLATES.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "bootstrap_required": False,
                "error": "Invalid username, password, or Google Authenticator code.",
                "bootstrap_data": None,
            },
            status_code=401,
        )
    auth_store.log_event("login_success", success=True, username=user.username)
    response = _redirect_to_default()
    _set_session_cookie(response, user, session_expiry(SETTINGS))
    return response


@app.post("/bootstrap", response_class=HTMLResponse, response_model=None)
def bootstrap_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    auth_store = get_auth_store()
    if not SETTINGS.auth_enabled:
        return _redirect_to_default()
    if auth_store.has_users():
        return _redirect_to_login()
    if password != confirm_password:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "bootstrap_required": True,
                "error": "Passwords do not match.",
                "bootstrap_data": None,
            },
            status_code=400,
        )
    try:
        user = auth_store.create_bootstrap_admin(username, password)
    except ValueError as exc:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="login.html",
            context={"request": request, "bootstrap_required": True, "error": str(exc), "bootstrap_data": None},
            status_code=400,
        )
    auth_store.log_event("bootstrap_admin_created", success=True, username=user.username)
    qr_buffer = BytesIO()
    auth_store.qr_image(user).save(qr_buffer, format="PNG")
    qr_b64 = base64.b64encode(qr_buffer.getvalue()).decode("ascii")
    return TEMPLATES.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "bootstrap_required": False,
            "error": "",
            "bootstrap_data": {
                "username": user.username,
                "provisioning_uri": auth_store.provisioning_uri(user),
                "totp_secret": user.totp_secret,
                "qr_b64": qr_b64,
            },
        },
    )


@app.post("/logout", response_model=None)
def logout(request: Request):
    auth_store = get_auth_store()
    session = _read_session(request, auth_store)
    if session.user:
        auth_store.log_event("logout", success=True, username=session.user.username)
    response = _redirect_to_login() if SETTINGS.auth_enabled else _redirect_to_default()
    _clear_session_cookie(response)
    return response


@app.get("/control", response_class=HTMLResponse, response_model=None)
def control_page(request: Request):
    auth_store = get_auth_store()
    try:
        session = _require_user(request, auth_store)
    except PermissionError:
        return _redirect_to_login()
    if SETTINGS.auth_enabled and session.user and not has_permission(session.user, "view_dashboard"):
        return _redirect_to_login()
    return _render_page(request, "control", session)


@app.get("/dashboard", response_class=HTMLResponse, response_model=None)
def dashboard_page(request: Request):
    auth_store = get_auth_store()
    try:
        session = _require_user(request, auth_store)
    except PermissionError:
        return _redirect_to_login()
    if SETTINGS.auth_enabled and session.user and not has_permission(session.user, "view_dashboard"):
        return _redirect_to_login()
    return _render_page(request, "dashboard", session)


def _authorized_session(request: Request, permission: str, action_label: str) -> tuple[Optional[AuthUser], AuthStore] | JSONResponse:
    auth_store = get_auth_store()
    try:
        session = _require_user(request, auth_store)
    except PermissionError:
        return JSONResponse({"error": "Authentication required."}, status_code=401)
    if session.user and not has_permission(session.user, permission):
        return _permission_denied(auth_store, session.user, permission, action_label)
    return session.user, auth_store


@app.post("/api/settings")
def update_runtime_settings(
    request: Request,
    trading_symbols: str = Form(...),
    invert_signals: Optional[str] = Form(None),
) -> JSONResponse:
    authorized = _authorized_session(request, "start_bot", "Update Settings")
    if isinstance(authorized, JSONResponse):
        return authorized
    user, auth_store = authorized
    engine = get_engine()
    if engine.status == "Running":
        response = _json_response(
            {"error": "Stop the bot before changing TRADING_SYMBOLS or INVERT_SIGNALS."},
            status_code=409,
        )
        if user:
            _set_session_cookie(response, user, session_expiry(SETTINGS))
        return response

    normalized_symbols = ",".join(symbol.strip().upper() for symbol in trading_symbols.split(",") if symbol.strip())
    if not normalized_symbols:
        response = _json_response({"error": "Enter at least one trading symbol."}, status_code=400)
        if user:
            _set_session_cookie(response, user, session_expiry(SETTINGS))
        return response

    normalized_invert = "true" if invert_signals == "true" else "false"
    _update_env_values(
        {
            "TRADING_SYMBOLS": normalized_symbols,
            "INVERT_SIGNALS": normalized_invert,
        }
    )
    _reload_runtime_settings()
    if user:
        auth_store.log_event(
            "settings_updated",
            success=True,
            username=user.username,
            details={
                "TRADING_SYMBOLS": normalized_symbols,
                "INVERT_SIGNALS": normalized_invert,
            },
        )
    response = _json_response(
        {
            "ok": True,
            "message": "Settings saved. The new values will be used on the next bot start.",
            "symbols": SETTINGS.symbols,
            "invert_signals": SETTINGS.invert_signals,
        }
    )
    if user:
        _set_session_cookie(response, user, session_expiry(SETTINGS))
    return response


@app.get("/api/control_snapshot")
def control_snapshot(request: Request) -> JSONResponse:
    auth_store = get_auth_store()
    try:
        session = _require_user(request, auth_store)
    except PermissionError:
        return _json_response({"error": "Authentication required."}, status_code=401)
    response = _json_response(get_engine().get_compact_snapshot())
    _refresh_session(response, session)
    return response


@app.get("/api/dashboard_snapshot")
def dashboard_snapshot(request: Request) -> JSONResponse:
    auth_store = get_auth_store()
    try:
        session = _require_user(request, auth_store)
    except PermissionError:
        return _json_response({"error": "Authentication required."}, status_code=401)
    response = _json_response(get_engine().get_snapshot())
    _refresh_session(response, session)
    return response


@app.post("/api/start")
def start_bot(request: Request) -> JSONResponse:
    authorized = _authorized_session(request, "start_bot", "Start Bot")
    if isinstance(authorized, JSONResponse):
        return authorized
    user, auth_store = authorized
    engine = get_engine()
    engine.start()
    if user:
        auth_store.log_event("bot_start", success=engine.status == "Running", username=user.username)
    response = _json_response({"ok": True, "status": engine.status, "last_error": engine.last_error})
    if user:
        _set_session_cookie(response, user, session_expiry(SETTINGS))
    return response


@app.post("/api/stop")
def stop_bot(request: Request) -> JSONResponse:
    authorized = _authorized_session(request, "stop_bot", "Stop Bot")
    if isinstance(authorized, JSONResponse):
        return authorized
    user, auth_store = authorized
    engine = get_engine()
    engine.stop()
    if user:
        auth_store.log_event("bot_stop", success=True, username=user.username)
    response = _json_response({"ok": True, "status": engine.status})
    if user:
        _set_session_cookie(response, user, session_expiry(SETTINGS))
    return response


@app.post("/api/close_position")
def close_position(request: Request, symbol: str = Form(...)) -> JSONResponse:
    authorized = _authorized_session(request, "stop_bot", "Close Open Position")
    if isinstance(authorized, JSONResponse):
        return authorized
    user, auth_store = authorized
    engine = get_engine()
    try:
        result = engine.close_open_position(symbol, reason="Manual close from web UI")
    except ValueError as exc:
        response = _json_response({"error": str(exc)}, status_code=404)
    except Exception as exc:
        response = _json_response({"error": str(exc)}, status_code=400)
    else:
        if user:
            auth_store.log_event(
                "position_closed_manual",
                success=True,
                username=user.username,
                details={"symbol": result["symbol"]},
            )
        response = _json_response({"ok": True, **result})
    if user:
        _set_session_cookie(response, user, session_expiry(SETTINGS))
    return response


@app.post("/api/backtest")
def run_backtest(request: Request, symbol: str = Form(...)) -> JSONResponse:
    authorized = _authorized_session(request, "run_backtest", "Run Backtest")
    if isinstance(authorized, JSONResponse):
        return authorized
    user, auth_store = authorized
    result = get_backtester().run(symbol)
    if user:
        auth_store.log_event("backtest_run", success=True, username=user.username, details={"symbol": symbol})
    payload = {
        "symbol": result.symbol,
        "total_return_pct": result.total_return_pct,
        "trades": result.trades,
        "win_rate_pct": result.win_rate_pct,
        "final_equity": result.final_equity,
        "max_drawdown_pct": result.max_drawdown_pct,
        "profit_factor": result.profit_factor,
        "avg_trade_pct": result.avg_trade_pct,
        "passed_filter": result.passed_filter,
        "filter_reason": result.filter_reason,
    }
    response = _json_response(payload)
    if user:
        _set_session_cookie(response, user, session_expiry(SETTINGS))
    return response
