from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pyotp
import qrcode

from config import Settings
from logger import append_jsonl

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "viewer": {"view_dashboard"},
    "analyst": {"view_dashboard", "run_backtest"},
    "operator": {"view_dashboard", "run_backtest", "start_bot", "stop_bot"},
    "admin": {"view_dashboard", "run_backtest", "start_bot", "stop_bot"},
}

PBKDF2_ITERATIONS = 390000


@dataclass(slots=True)
class AuthUser:
    username: str
    role: str
    password_hash: str
    totp_secret: str
    disabled: bool = False
    created_at: str = ""

    @property
    def permissions(self) -> set[str]:
        return ROLE_PERMISSIONS.get(self.role, set())


class AuthStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.users_path = settings.auth_users_file
        self.users_path.parent.mkdir(parents=True, exist_ok=True)

    def load_users(self) -> dict[str, AuthUser]:
        if not self.users_path.exists():
            return {}
        payload = json.loads(self.users_path.read_text(encoding="utf-8"))
        users: dict[str, AuthUser] = {}
        for item in payload:
            user = AuthUser(**item)
            users[user.username.lower()] = user
        return users

    def save_users(self, users: dict[str, AuthUser]) -> None:
        payload = [asdict(user) for user in sorted(users.values(), key=lambda user: user.username.lower())]
        self.users_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get_user(self, username: str) -> Optional[AuthUser]:
        return self.load_users().get(username.strip().lower())

    def has_users(self) -> bool:
        return bool(self.load_users())

    def create_bootstrap_admin(self, username: str, password: str) -> AuthUser:
        normalized = username.strip()
        if len(normalized) < 3:
            raise ValueError("Username must be at least 3 characters long.")
        if len(password) < 10:
            raise ValueError("Password must be at least 10 characters long.")
        users = self.load_users()
        if users:
            raise ValueError("Bootstrap is only available before the first user is created.")
        user = AuthUser(
            username=normalized,
            role="admin",
            password_hash=hash_password(password),
            totp_secret=pyotp.random_base32(),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        users[normalized.lower()] = user
        self.save_users(users)
        return user

    def verify_login(self, username: str, password: str, totp_code: str) -> Optional[AuthUser]:
        user = self.get_user(username)
        if not user or user.disabled:
            return None
        if not verify_password(password, user.password_hash):
            return None
        normalized_code = "".join(char for char in totp_code if char.isdigit())
        if not normalized_code or not pyotp.TOTP(user.totp_secret).verify(normalized_code, valid_window=1):
            return None
        return user

    def provisioning_uri(self, user: AuthUser) -> str:
        return pyotp.TOTP(user.totp_secret).provisioning_uri(name=user.username, issuer_name=self.settings.auth_totp_issuer)

    def qr_image(self, user: AuthUser) -> Any:
        qr = qrcode.make(self.provisioning_uri(user))
        return qr.get_image() if hasattr(qr, "get_image") else qr

    def log_event(
        self,
        action: str,
        success: bool,
        username: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        append_jsonl(
            self.settings.auth_audit_log_path,
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "success": success,
                "username": username or "",
                "details": details or {},
            },
        )


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        [
            "pbkdf2_sha256",
            str(iterations),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        salt = base64.b64decode(raw_salt.encode("ascii"))
        expected_digest = base64.b64decode(raw_digest.encode("ascii"))
    except Exception:
        return False
    actual_digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual_digest, expected_digest)


def session_expiry(settings: Settings) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=settings.auth_session_minutes)


def has_permission(user: AuthUser, permission: str) -> bool:
    return permission in user.permissions
