from __future__ import annotations

import json
import mimetypes
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from app.config import ROOT_DIR, api_key_expirations, platform_configs
from app.repository import Repository
from app.services import TradingService
from app.strategy_capabilities import strategy_capabilities
from app.validation import validate_strategy


STATIC_DIR = ROOT_DIR / "static"
repo = Repository()
service = TradingService(repo)


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingDashboardMVP/0.1"

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
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self.json_response({"ok": True, "mode": "dry_run"})
        if parsed.path == "/api/platforms":
            return self.json_response(service.platforms())
        if parsed.path == "/api/portfolio/summary":
            return self.json_response(service.portfolio_summary())
        if parsed.path == "/api/sync/status":
            return self.json_response(
                {
                    "latest": repo.latest_sync_runs(),
                    "history": repo.recent_sync_runs(),
                    "api_keys": api_key_expirations(),
                }
            )
        if parsed.path == "/api/orders":
            return self.json_response(repo.orders())
        if parsed.path == "/api/strategies":
            return self.json_response(repo.strategies())
        if parsed.path == "/api/strategy-capabilities":
            return self.json_response(strategy_capabilities())
        if parsed.path == "/api/asset-aliases":
            return self.json_response(repo.asset_aliases())
        return self.static_response(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            data = self.read_json()
        except ValueError as exc:
            return self.json_response({"error": str(exc)}, status=400)
        if parsed.path == "/api/orders":
            return self.json_response({"error": "수동 주문은 지원하지 않습니다. 전략 실행 엔진에서만 주문 기록을 생성합니다."}, status=405)
        if parsed.path == "/api/sync/upbit":
            result = service.sync_upbit_holdings()
            return self.json_response(result, status=sync_http_status(result))
        if parsed.path == "/api/sync/kis":
            result = service.sync_kis_holdings()
            return self.json_response(result, status=sync_http_status(result))
        if parsed.path == "/api/sync/toss":
            result = service.sync_toss_holdings()
            return self.json_response(result, status=sync_http_status(result))
        if parsed.path == "/api/sync/all":
            result = service.sync_all_holdings()
            return self.json_response(result, status=sync_http_status(result))
        if parsed.path == "/api/strategies":
            try:
                request = validate_strategy(data)
            except ValueError as exc:
                return self.json_response({"error": str(exc)}, status=400)
            created = repo.create_strategy(request)
            return self.json_response(created, status=201)
        return self.json_response({"error": "not found"}, status=404)

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/strategies/") and parsed.path.endswith("/enabled"):
            parts = parsed.path.strip("/").split("/")
            try:
                strategy_id = int(parts[2])
            except (IndexError, ValueError):
                return self.json_response({"error": "잘못된 전략 ID입니다."}, status=400)
            enabled = parse_qs(parsed.query).get("value", ["false"])[0].lower() == "true"
            item = repo.set_strategy_enabled(strategy_id, enabled)
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
            updated = repo.update_strategy(strategy_id, request)
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
        return self.json_response(repo.set_asset_alias(platform, symbol, alias))

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
            if not repo.delete_strategy(strategy_id):
                return self.json_response({"error": "not found"}, status=404)
            return self.json_response({"deleted": True})

        alias_target = parse_alias_target(parsed.path)
        if not alias_target:
            return self.json_response({"error": "not found"}, status=404)
        platform, symbol = alias_target
        if platform not in {item.code for item in platform_configs()}:
            return self.json_response({"error": "지원하지 않는 플랫폼입니다."}, status=400)
        return self.json_response({"deleted": repo.delete_asset_alias(platform, symbol)})

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
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Trading dashboard MVP running at http://{host}:{port}")
    server.serve_forever()


def sync_http_status(result: dict) -> int:
    if result.get("status") == "success":
        return 200
    if result.get("status") == "partial":
        return 207
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
