from __future__ import annotations

import json
import mimetypes
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from app.config import ROOT_DIR, api_key_expirations, platform_configs
from app.repository import Repository
from app.scheduler import StrategyScheduler
from app.services import TradingService
from app.strategy_capabilities import strategy_capabilities
from app.validation import validate_strategy


STATIC_DIR = ROOT_DIR / "static"
repo = Repository()
service = TradingService(repo)


class DashboardHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, repository: Repository, trading_service: TradingService):
        super().__init__(server_address, handler_class)
        self.repository = repository
        self.trading_service = trading_service


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    repository: Repository | None = None,
    trading_service: TradingService | None = None,
) -> DashboardHTTPServer:
    repository = repository or Repository()
    trading_service = trading_service or TradingService(repository)
    return DashboardHTTPServer((host, port), Handler, repository, trading_service)


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingDashboardMVP/0.1"

    @property
    def repository(self) -> Repository:
        return self.server.repository

    @property
    def trading_service(self) -> TradingService:
        return self.server.trading_service

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        target = STATIC_DIR / ("index.html" if parsed.path in ("", "/") else parsed.path.lstrip("/"))
        if not target.exists() or not target.is_file() or STATIC_DIR not in target.resolve().parents:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self.json_response(
                {
                    "ok": True,
                    "mode": "dry_run",
                    "active_alerts": self.repository.unresolved_alert_count(),
                }
            )
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
        try:
            data = self.read_json()
        except ValueError as exc:
            return self.json_response({"error": str(exc)}, status=400)
        if parsed.path == "/api/orders":
            return self.json_response({"error": "수동 주문은 지원하지 않습니다. 전략 실행 엔진에서만 주문 기록을 생성합니다."}, status=405)
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

    def do_PATCH(self) -> None:
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

    def json_response(self, data, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def static_response(self, path: str) -> None:
        target = STATIC_DIR / ("index.html" if path in ("", "/") else path.lstrip("/"))
        if not target.exists() or not target.is_file() or STATIC_DIR not in target.resolve().parents:
            return self.json_response({"error": "not found"}, status=404)
        content = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            content_type = "text/javascript"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(host, port, repository=repo, trading_service=service)
    scheduler = StrategyScheduler(service)
    scheduler.start()
    print(f"Trading dashboard MVP running at http://{host}:{port}")
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
