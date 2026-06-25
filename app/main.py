from __future__ import annotations

import json
import mimetypes
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.config import ROOT_DIR
from app.integrations.kis import KISError
from app.integrations.tossinvest import TossInvestError
from app.integrations.upbit import UpbitError
from app.repository import Repository
from app.services import TradingService


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
        if parsed.path == "/api/orders":
            return self.json_response(repo.orders())
        if parsed.path == "/api/strategies":
            return self.json_response(repo.strategies())
        return self.static_response(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        data = self.read_json()
        if parsed.path == "/api/orders":
            return self.json_response({"error": "수동 주문은 지원하지 않습니다. 전략 실행 엔진에서만 주문 기록을 생성합니다."}, status=405)
        if parsed.path == "/api/sync/upbit":
            try:
                return self.json_response(service.sync_upbit_holdings())
            except UpbitError as exc:
                return self.json_response({"error": str(exc)}, status=502)
        if parsed.path == "/api/sync/kis":
            try:
                return self.json_response(service.sync_kis_holdings())
            except KISError as exc:
                return self.json_response({"error": str(exc)}, status=502)
        if parsed.path == "/api/sync/toss":
            try:
                return self.json_response(service.sync_toss_holdings())
            except TossInvestError as exc:
                return self.json_response({"error": str(exc)}, status=502)
        if parsed.path == "/api/sync/all":
            return self.json_response(service.sync_all_holdings())
        if parsed.path == "/api/strategies":
            try:
                created = repo.create_strategy(data)
            except KeyError:
                return self.json_response({"error": "전략 이름은 필수입니다."}, status=400)
            return self.json_response(created, status=201)
        return self.json_response({"error": "not found"}, status=404)

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/strategies/") and parsed.path.endswith("/enabled"):
            parts = parsed.path.strip("/").split("/")
            strategy_id = int(parts[2])
            enabled = parse_qs(parsed.query).get("value", ["false"])[0].lower() == "true"
            item = repo.set_strategy_enabled(strategy_id, enabled)
            if not item:
                return self.json_response({"error": "not found"}, status=404)
            return self.json_response(item)
        return self.json_response({"error": "not found"}, status=404)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    run(args.host, args.port)
