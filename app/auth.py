from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Mapping

from app.config import env_flag, load_env


SESSION_COOKIE = "td_session"
CSRF_COOKIE = "td_csrf"
PASSWORD_SCHEME = "pbkdf2_sha256"
DEFAULT_PASSWORD_ITERATIONS = 310_000
SESSION_TTL_SECONDS = 8 * 60 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 60


class AuthConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthUser:
    username: str
    role: str


@dataclass(frozen=True)
class AuthSession:
    token: str
    csrf_token: str
    user: AuthUser
    expires_at: float


class AuthManager:
    def __init__(
        self,
        *,
        enabled: bool | None = None,
        viewer_password_hash: str | None = None,
        operator_password_hash: str | None = None,
        session_ttl_seconds: int | None = None,
        cookie_secure: bool | None = None,
    ) -> None:
        load_env()
        self.enabled = env_flag("TRADING_DASHBOARD_AUTH_ENABLED", False) if enabled is None else enabled
        self.viewer_password_hash = (
            viewer_password_hash
            if viewer_password_hash is not None
            else os.getenv("TRADING_DASHBOARD_VIEWER_PASSWORD_HASH", "").strip()
        )
        self.operator_password_hash = (
            operator_password_hash
            if operator_password_hash is not None
            else os.getenv("TRADING_DASHBOARD_OPERATOR_PASSWORD_HASH", "").strip()
        )
        self.session_ttl_seconds = session_ttl_seconds or _env_int(
            "TRADING_DASHBOARD_SESSION_TTL_SECONDS",
            SESSION_TTL_SECONDS,
            minimum=300,
            maximum=7 * 24 * 60 * 60,
        )
        self.cookie_secure = (
            env_flag("TRADING_DASHBOARD_COOKIE_SECURE", False)
            if cookie_secure is None
            else cookie_secure
        )
        self._sessions: dict[str, AuthSession] = {}
        self._failed_logins: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.viewer_password_hash and self.operator_password_hash)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "session_ttl_seconds": self.session_ttl_seconds,
        }

    def authenticate(self, username: str, password: str, *, client_key: str = "unknown") -> AuthSession:
        if not self.enabled:
            return AuthSession(
                token="",
                csrf_token="",
                user=AuthUser("local", "operator"),
                expires_at=float("inf"),
            )
        if not self.configured:
            raise AuthConfigurationError(
                "인증이 활성화되었지만 viewer/operator 비밀번호 해시가 설정되지 않았습니다."
            )
        if not self.login_allowed(client_key):
            raise PermissionError("로그인 시도가 일시적으로 제한되었습니다.")

        password_hash = {
            "viewer": self.viewer_password_hash,
            "operator": self.operator_password_hash,
        }.get(username)
        if not password_hash or not verify_password(password, password_hash):
            self.record_login_failure(client_key)
            raise PermissionError("사용자 이름 또는 비밀번호가 올바르지 않습니다.")

        self.clear_login_failures(client_key)
        session = AuthSession(
            token=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(24),
            user=AuthUser(username, username),
            expires_at=time.time() + self.session_ttl_seconds,
        )
        with self._lock:
            self._sessions[session.token] = session
        return session

    def session_from_headers(self, headers: Mapping[str, str]) -> AuthSession | None:
        if not self.enabled:
            return AuthSession(
                token="",
                csrf_token="",
                user=AuthUser("local", "operator"),
                expires_at=float("inf"),
            )
        token = _session_token(headers)
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session and session.expires_at > time.time():
                return session
            self._sessions.pop(token, None)
        return None

    def csrf_valid(self, headers: Mapping[str, str], session: AuthSession | None) -> bool:
        if not self.enabled or session is None:
            return not self.enabled
        supplied = headers.get("X-CSRF-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, session.csrf_token)

    def revoke(self, session: AuthSession | None) -> None:
        if not session or not session.token:
            return
        with self._lock:
            self._sessions.pop(session.token, None)

    def login_allowed(self, client_key: str) -> bool:
        now = time.time()
        with self._lock:
            failures = self._failed_logins.get(client_key)
            if not failures:
                return True
            count, blocked_until = failures
            if blocked_until <= now:
                self._failed_logins.pop(client_key, None)
                return True
            return count < LOGIN_FAILURE_LIMIT

    def record_login_failure(self, client_key: str) -> None:
        now = time.time()
        with self._lock:
            count, blocked_until = self._failed_logins.get(client_key, (0, now))
            if blocked_until <= now:
                count = 0
            count += 1
            self._failed_logins[client_key] = (count, now + LOGIN_FAILURE_WINDOW_SECONDS)

    def clear_login_failures(self, client_key: str) -> None:
        with self._lock:
            self._failed_logins.pop(client_key, None)

    def session_cookie_headers(self, session: AuthSession) -> list[str]:
        return [
            _cookie_header(SESSION_COOKIE, session.token, http_only=True, max_age=self.session_ttl_seconds, secure=self.cookie_secure),
            _cookie_header(CSRF_COOKIE, session.csrf_token, http_only=False, max_age=self.session_ttl_seconds, secure=self.cookie_secure),
        ]

    def clear_cookie_headers(self) -> list[str]:
        return [
            _cookie_header(SESSION_COOKIE, "", http_only=True, max_age=0, secure=self.cookie_secure),
            _cookie_header(CSRF_COOKIE, "", http_only=False, max_age=0, secure=self.cookie_secure),
        ]


def hash_password(password: str, *, iterations: int = DEFAULT_PASSWORD_ITERATIONS) -> str:
    if not password:
        raise ValueError("비밀번호는 비워둘 수 없습니다.")
    if iterations < 100_000:
        raise ValueError("PBKDF2 반복 횟수는 100000 이상이어야 합니다.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        [
            PASSWORD_SCHEME,
            str(iterations),
            base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
            base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(raw_iterations)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(raw_salt + "=" * (-len(raw_salt) % 4))
        expected = base64.urlsafe_b64decode(raw_digest + "=" * (-len(raw_digest) % 4))
    except (TypeError, ValueError, binascii.Error):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _session_token(headers: Mapping[str, str]) -> str:
    authorization = headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("Cookie", ""))
    except Exception:
        return ""
    return cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else ""


def _cookie_header(
    name: str,
    value: str,
    *,
    http_only: bool,
    max_age: int,
    secure: bool,
) -> str:
    cookie = SimpleCookie()
    cookie[name] = value
    cookie[name]["Path"] = "/"
    cookie[name]["Max-Age"] = str(max_age)
    cookie[name]["SameSite"] = "Strict"
    if http_only:
        cookie[name]["HttpOnly"] = True
    if secure:
        cookie[name]["Secure"] = True
    return cookie.output(header="").strip()


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def main() -> None:
    parser = argparse.ArgumentParser(description="Trading Dashboard 인증 유틸리티")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hash-password", help="입력한 비밀번호의 PBKDF2 해시 출력")
    args = parser.parse_args()
    if args.command == "hash-password":
        first = getpass.getpass("비밀번호: ")
        second = getpass.getpass("비밀번호 확인: ")
        if first != second:
            parser.error("두 비밀번호가 일치하지 않습니다.")
        print(hash_password(first))


if __name__ == "__main__":
    main()
