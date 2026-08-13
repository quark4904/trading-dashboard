from __future__ import annotations

import json
import mimetypes
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from app.auth import AuthConfigurationError, AuthManager
from app.config import ROOT_DIR, api_key_expirations, platform_configs
from app.repository import Repository
from app.scheduler import StrategyScheduler
from app.services import TradingService
from app.observability import configure_logging
from app.strategy_capabilities import strategy_capabilities
from app.validation import validate_strategy


STATIC_DIR = ROOT_DIR / "static"
logger = configure_logging()
repo = Repository()
service = TradingService(repo)
auth_manager = AuthManager()

PUBLIC_STATIC_PATHS = {"/login", "/login.html", "/login.js", "/login.css"}
READ_ONLY_ROLE = "viewer"
MUTATING_ROLE = "operator"


class DashboardHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address,
        handler_class,
        repository: Repository,
        trading_service: TradingService,
        authentication: AuthManager,
    ):
        super().__init__(server_address, handler_class)
        self.repository = repository
        self.trading_service = trading_service
        self.authentication = authentication


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    repository: Repository | None = None,
    trading_service: TradingService | None = None,
    authentication: AuthManager | None = None,
) -> DashboardHTTPServer:
    repository = repository or Repository()
    trading_service = trading_service or TradingService(repository)
    authentication = authentication or AuthManager()
    return DashboardHTTPServer((host, port), Handler, repository, trading_service, authentication)


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingDashboardMVP/0.1"

    @property
    def repository(self) -> Repository:
        return self.server.repository

    @property
    def trading_service(self) -> TradingService:
        return self.server.trading_service

    @property
    def authentication(self) -> AuthManager:
        return getattr(self.server, "authentication", auth_manager)

    def _authorize(self, required_role: str = READ_ONLY_ROLE, *, mutation: bool = False) -> bool:
        headers = getattr(self, "headers", {})
        try:
            if self.authentication.enabled and not self.authentication.configured:
                raise AuthConfigurationError(
                    "인증이 활성화되었지만 viewer/operator 비밀번호 해시가 설정되지 않았습니다."
                )
            session = self.authentication.session_from_headers(headers)
        except AuthConfigurationError as exc:
            if self.path.startswith("/api/"):
                self.json_response({"error": str(exc)}, status=503)
            else:
                self.json_response({"error": str(exc)}, status=503)
            return False

        if session is None:
            if self.path.startswith("/api/"):
                self.json_response(
                    {"error": "로그인이 필요합니다."},
                    status=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            else:
                self.redirect("/login")
            return False

        if required_role == MUTATING_ROLE and session.user.role != MUTATING_ROLE:
            self.json_response({"error": "operator 권한이 필요합니다."}, status=403)
            return False
        if mutation and not self.authentication.csrf_valid(headers, session):
            self.json_response({"error": "CSRF 토큰이 없거나 올바르지 않습니다."}, status=403)
            return False
        self.current_session = session
        return True

    def _client_key(self) -> str:
        address = getattr(self, "client_address", ("unknown", 0))
        return str(address[0]) if address else "unknown"

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in PUBLIC_STATIC_PATHS and not self.authentication.enabled:
            self.redirect("/")
            return
        if parsed.path not in PUBLIC_STATIC_PATHS and not self._authorize(READ_ONLY_ROLE):
            return
        target = STATIC_DIR / ("index.html" if parsed.path in ("", "/") else parsed.path.lstrip("/"))
        if not target.exists() or not target.is_file() or STATIC_DIR not in target.resolve().parents:
            self.send_response(404)
            self.security_headers()
            self.end_headers()
            return
        self.send_response(200)
        self.security_headers()
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in PUBLIC_STATIC_PATHS:
            if not self.authentication.enabled:
                return self.redirect("/")
            return self.static_response("/login.html" if parsed.path == "/login" else parsed.path)
        if parsed.path == "/api/auth/me":
            return self.auth_me()
        if parsed.path == "/api/health":
            database = self.repository.health_status()
            auth = self.authentication.status()
            ready = database["ok"] and (not auth["enabled"] or auth["configured"])
            return self.json_response(
                {
                    "ok": ready,
                    "mode": "dry_run",
                    "active_alerts": self.repository.unresolved_alert_count(),
                    "database": database,
                    "auth": auth,
                },
                status=200 if ready else 503,
            )
        if not self._authorize(READ_ONLY_ROLE):
            return
        if parsed.path == "/api/platforms":
            return self.json_response(self.trading_service.platforms())
        if parsed.path == "/api/portfolio/summary":
            return self.json_response(self.trading_service.portfolio_summary())
        if parsed.path == "/api/sync/status":
            return self.json_response(
                {
                    "latest": self.repository.latest_sync_runs(),
                    "history": self.repository.recent_sync_runs(),
                    "api_keys": api_key_expirations(),
                    "alerts": self.repository.alerts(),
                    "locks": self.repository.operation_locks(),
                }
            )
        if parsed.path == "/api/alerts":
            include_acknowledged = parse_qs(parsed.query).get("include_acknowledged", ["false"])[0].lower() == "true"
            return self.json_response(self.repository.alerts(include_acknowledged=include_acknowledged))
        if parsed.path == "/api/maintenance/migrations":
            return self.json_response(
                {
                    "schema_version": self.repository.schema_version(),
                    "history": self.repository.migration_history(),
                }
            )
        if parsed.path == "/api/orders":
            return self.json_response(self.repository.orders())
        if parsed.path == "/api/executions":
            return self.json_response(self.repository.executions())
        if parsed.path == "/api/strategy-runs":
            return self.json_response(self.repository.strategy_runs())
        if parsed.path == "/api/strategies":
            return self.json_response(self.repository.strategies())
        if parsed.path == "/api/strategy-capabilities":
            return self.json_response(strategy_capabilities())
        if parsed.path == "/api/asset-aliases":
            return self.json_response(self.repository.asset_aliases())
        return self.static_response(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/login":
            if not self.authentication.enabled:
                return self.json_response(
                    {"error": "이 배포는 Cloudflare Access 인증을 사용합니다."},
                    status=404,
                )
            return self.login()
        if parsed.path == "/api/auth/logout":
            session = self.authentication.session_from_headers(getattr(self, "headers", {}))
            if session and not self.authentication.csrf_valid(getattr(self, "headers", {}), session):
                return self.json_response({"error": "CSRF 토큰이 없거나 올바르지 않습니다."}, status=403)
            self.authentication.revoke(session)
            return self.json_response(
                {"logged_out": True},
                cookies=self.authentication.clear_cookie_headers(),
            )
        if not self._authorize(MUTATING_ROLE, mutation=True):
            return
        try:
            data = self.read_json()
        except ValueError as exc:
            return self.json_response({"error": str(exc)}, status=400)
        if parsed.path == "/api/orders":
            return self.json_response({"error": "수동 주문은 지원하지 않습니다. 전략 실행 엔진에서만 주문 기록을 생성합니다."}, status=405)
        if parsed.path == "/api/backtests":
            try:
                strategy_id = int(data.get("strategy_id"))
                bars = data.get("bars")
                initial_cash = data.get("initial_cash")
                result = self.trading_service.run_dca_backtest(strategy_id, bars, initial_cash)
            except (TypeError, ValueError) as exc:
                return self.json_response({"error": str(exc)}, status=400)
            return self.json_response(result, status=201)
        if parsed.path == "/api/strategy-runs/execute-due":
            return self.json_response(self.trading_service.run_due_dca_strategies())
        if parsed.path.startswith("/api/strategies/") and parsed.path.endswith("/dry-run"):
            parts = parsed.path.strip("/").split("/")
            try:
                strategy_id = int(parts[2])
                result = self.trading_service.run_dca_strategy_now(strategy_id)
            except (IndexError, ValueError) as exc:
                return self.json_response({"error": str(exc)}, status=400)
            status = 201 if result.get("status") == "success" else 422
            payload = result if status == 201 else {"error": result.get("error") or "주문 전 검증에 실패했습니다.", "run": result}
            return self.json_response(payload, status=status)
        if parsed.path == "/api/sync/upbit":
            result = self.trading_service.sync_upbit_holdings()
            return self.json_response(result, status=sync_http_status(result))
        if parsed.path == "/api/sync/kis":
            result = self.trading_service.sync_kis_holdings()
            return self.json_response(result, status=sync_http_status(result))
        if parsed.path == "/api/sync/toss":
            result = self.trading_service.sync_toss_holdings()
            return self.json_response(result, status=sync_http_status(result))
        if parsed.path == "/api/sync/all":
            result = self.trading_service.sync_all_holdings()
            return self.json_response(result, status=sync_http_status(result))
        if parsed.path == "/api/strategies":
            try:
                request = validate_strategy(data)
            except ValueError as exc:
                return self.json_response({"error": str(exc)}, status=400)
            created = self.repository.create_strategy(request)
            return self.json_response(created, status=201)
        return self.json_response({"error": "not found"}, status=404)

    def login(self) -> None:
        try:
            data = self.read_json()
        except ValueError as exc:
            return self.json_response({"error": str(exc)}, status=400)
        username = str(data.get("username") or "").strip()
        password = data.get("password")
        if username not in {"viewer", "operator"} or not isinstance(password, str) or not password:
            return self.json_response({"error": "사용자 이름과 비밀번호를 확인해 주세요."}, status=400)

        client_key = self._client_key()
        if not self.authentication.login_allowed(client_key):
            return self.json_response({"error": "로그인 시도가 일시적으로 제한되었습니다."}, status=429)
        try:
            session = self.authentication.authenticate(username, password, client_key=client_key)
        except AuthConfigurationError as exc:
            return self.json_response({"error": str(exc)}, status=503)
        except PermissionError as exc:
            return self.json_response({"error": str(exc)}, status=401)
        return self.json_response(
            {
                "authenticated": True,
                "user": {"username": session.user.username, "role": session.user.role},
            },
            cookies=self.authentication.session_cookie_headers(session),
        )

    def auth_me(self) -> None:
        if self.authentication.enabled and not self.authentication.configured:
            return self.json_response(
                {"authenticated": False, "auth": self.authentication.status()},
                status=503,
            )
        session = self.authentication.session_from_headers(getattr(self, "headers", {}))
        if session is None:
            return self.json_response({"authenticated": False}, status=401)
        return self.json_response(
            {
                "authenticated": True,
                "auth": self.authentication.status(),
                "user": {"username": session.user.username, "role": session.user.role},
            }
        )

    def do_PATCH(self) -> None:
        if not self._authorize(MUTATING_ROLE, mutation=True):
            return
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/alerts/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 3 or parts[1] != "alerts":
                return self.json_response({"error": "not found"}, status=404)
            try:
                alert_id = int(parts[2])
            except ValueError:
                return self.json_response({"error": "잘못된 알림 ID입니다."}, status=400)
            acknowledged = parse_qs(parsed.query).get("acknowledged", ["true"])[0].lower() == "true"
            if not acknowledged:
                return self.json_response({"error": "알림은 확인 처리만 지원합니다."}, status=400)
            item = self.repository.acknowledge_alert(alert_id)
            if not item:
                return self.json_response({"error": "not found"}, status=404)
            return self.json_response(item)
        if parsed.path.startswith("/api/strategies/") and parsed.path.endswith("/enabled"):
            parts = parsed.path.strip("/").split("/")
            try:
                strategy_id = int(parts[2])
            except (IndexError, ValueError):
                return self.json_response({"error": "잘못된 전략 ID입니다."}, status=400)
            enabled = parse_qs(parsed.query).get("value", ["false"])[0].lower() == "true"
            item = self.repository.set_strategy_enabled(strategy_id, enabled)
            if not item:
                return self.json_response({"error": "not found"}, status=404)
            return self.json_response(item)
        return self.json_response({"error": "not found"}, status=404)

    def do_PUT(self) -> None:
        if not self._authorize(MUTATING_ROLE, mutation=True):
            return
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/strategies/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 3:
                return self.json_response({"error": "not found"}, status=404)
            try:
                strategy_id = int(parts[2])
                request = validate_strategy(self.read_json())
            except ValueError as exc:
                return self.json_response({"error": str(exc)}, status=400)
            updated = self.repository.update_strategy(strategy_id, request)
            if not updated:
                return self.json_response({"error": "not found"}, status=404)
            return self.json_response(updated)

        alias_target = parse_alias_target(parsed.path)
        if not alias_target:
            return self.json_response({"error": "not found"}, status=404)

        try:
            data = self.read_json()
        except ValueError as exc:
            return self.json_response({"error": str(exc)}, status=400)
        alias = str(data.get("alias") or "").strip()
        if not alias:
            return self.json_response({"error": "별칭은 비워둘 수 없습니다."}, status=400)
        if len(alias) > 100:
            return self.json_response({"error": "별칭은 100자 이하여야 합니다."}, status=400)

        platform, symbol = alias_target
        if platform not in {item.code for item in platform_configs()}:
            return self.json_response({"error": "지원하지 않는 플랫폼입니다."}, status=400)
        if len(symbol) > 50:
            return self.json_response({"error": "종목 코드는 50자 이하여야 합니다."}, status=400)
        return self.json_response(self.repository.set_asset_alias(platform, symbol, alias))

    def do_DELETE(self) -> None:
        if not self._authorize(MUTATING_ROLE, mutation=True):
            return
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/strategies/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 3:
                return self.json_response({"error": "not found"}, status=404)
            try:
                strategy_id = int(parts[2])
            except ValueError:
                return self.json_response({"error": "잘못된 전략 ID입니다."}, status=400)
            if not self.repository.delete_strategy(strategy_id):
                return self.json_response({"error": "not found"}, status=404)
            return self.json_response({"deleted": True})

        alias_target = parse_alias_target(parsed.path)
        if not alias_target:
            return self.json_response({"error": "not found"}, status=404)
        platform, symbol = alias_target
        if platform not in {item.code for item in platform_configs()}:
            return self.json_response({"error": "지원하지 않는 플랫폼입니다."}, status=400)
        return self.json_response({"deleted": self.repository.delete_asset_alias(platform, symbol)})

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("잘못된 Content-Length입니다.") from exc
        if length < 0 or length > 65_536:
            raise ValueError("요청 본문은 64KB 이하여야 합니다.")
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("올바른 JSON 요청이 아닙니다.") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON 객체 형식이 필요합니다.")
        return data

    def json_response(
        self,
        data,
        status: int = 200,
        *,
        headers: dict[str, str] | None = None,
        cookies: list[str] | None = None,
    ) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.security_headers()
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; script-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'",
        )

    def static_response(self, path: str) -> None:
        target = STATIC_DIR / ("index.html" if path in ("", "/") else path.lstrip("/"))
        if not target.exists() or not target.is_file() or STATIC_DIR not in target.resolve().parents:
            return self.json_response({"error": "not found"}, status=404)
        content = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            content_type = "text/javascript"
        self.send_response(200)
        self.security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), format % args)


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(host, port, repository=repo, trading_service=service)
    scheduler = StrategyScheduler(service)
    scheduler.start()
    logger.info("Trading dashboard MVP running at http://%s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        scheduler.stop()


def sync_http_status(result: dict) -> int:
    if result.get("status") == "success":
        return 200
    if result.get("status") == "partial":
        return 207
    if result.get("status") == "busy":
        return 409
    return 502


def parse_alias_target(path: str) -> tuple[str, str] | None:
    prefix = "/api/asset-aliases/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix) :].split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return unquote(parts[0]), unquote(parts[1])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    run(args.host, args.port)
