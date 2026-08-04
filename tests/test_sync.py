from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request
from unittest.mock import MagicMock, patch

from app.auth import AuthManager, hash_password, verify_password
from app.config import api_key_expirations
from app.fee_policies import FeePolicyStore
from app.integrations.fx import FxError, usd_krw_rate
from app.integrations.http import RateLimiter, RetryPolicy, request_json
from app.integrations.kis import KISClient
from app.integrations.tossinvest import TossInvestClient
from app.integrations.upbit import UpbitClient
from app.order_costs import estimate_dca_buy_cost
from app.repository import Repository
from app.scheduler import KST, scheduled_slot
from app.services import TradingService, _normalize_upbit_execution
from app.strategy_capabilities import compile_dca_buy_request, strategy_capabilities
from app.validation import validate_strategy


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_seed = os.environ.pop("TRADING_DASHBOARD_SEED_DEMO", None)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "dashboard.db"

    def tearDown(self) -> None:
        if self.previous_seed is not None:
            os.environ["TRADING_DASHBOARD_SEED_DEMO"] = self.previous_seed
        else:
            os.environ.pop("TRADING_DASHBOARD_SEED_DEMO", None)
        self.temp_dir.cleanup()

    def test_new_database_is_empty_by_default(self) -> None:
        repo = Repository(self.db_path)

        self.assertEqual(repo.holdings(), [])
        self.assertEqual(repo.strategies(), [])

    def test_demo_seed_requires_explicit_flag(self) -> None:
        os.environ["TRADING_DASHBOARD_SEED_DEMO"] = "true"

        repo = Repository(self.db_path)

        self.assertGreater(len(repo.holdings()), 0)
        self.assertGreater(len(repo.strategies()), 0)

    def test_sync_run_is_persisted(self) -> None:
        repo = Repository(self.db_path)
        sync_id = repo.start_sync("upbit")

        run = repo.finish_sync(sync_id, status="success", synced_count=3, execution_count=2)

        self.assertEqual(run["status"], "success")
        self.assertEqual(run["synced_count"], 3)
        self.assertEqual(run["execution_count"], 2)
        self.assertEqual(repo.latest_sync_runs()[0]["platform"], "upbit")
        self.assertEqual(repo.recent_sync_runs()[0]["id"], sync_id)

    def test_execution_upsert_is_idempotent_and_prefers_actual_fee(self) -> None:
        repo = Repository(self.db_path)
        row = {
            "external_order_id": "order-1",
            "ordered_at": "2026-07-30T09:00:00+09:00",
            "executed_at": "2026-07-30T09:00:01+09:00",
            "symbol": "005930",
            "name": "삼성전자",
            "side": "buy",
            "order_type": "market",
            "status": "filled",
            "quantity": 1,
            "average_price": 70000,
            "amount": 70000,
            "currency": "KRW",
            "actual_fee": None,
            "estimated_fee": 10.5,
            "actual_tax": None,
            "estimated_tax": 0,
            "cost_profile": {"fee_source": {"kind": "official_policy", "label": "공식 요율"}},
        }
        repo.upsert_executions("toss", [row])
        row.update(
            {
                "actual_fee": 10,
                "estimated_fee": None,
                "cost_profile": {"fee_source": {"kind": "actual_api", "label": "체결 이력"}},
            }
        )
        repo.upsert_executions("toss", [row])
        row.update(
            {
                "actual_fee": None,
                "estimated_fee": 10.5,
                "cost_profile": {"fee_source": {"kind": "official_policy", "label": "공식 요율"}},
            }
        )
        repo.upsert_executions("toss", [row])

        executions = repo.executions()

        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0]["fee"], 10)
        self.assertEqual(executions[0]["fee_status"], "actual")
        self.assertEqual(executions[0]["cost_profile"]["fee_source"]["kind"], "actual_api")

    def test_asset_alias_survives_holding_replacement(self) -> None:
        repo = Repository(self.db_path)
        row = {
            "symbol": "SCHD",
            "name": "SCHD",
            "asset_type": "stock",
            "quantity": 1,
            "avg_price": 10,
            "current_price": 11,
            "currency": "USD",
        }
        repo.set_asset_alias("toss", "SCHD", "슈왑 미국 배당주 ETF")
        repo.replace_platform_holdings("toss", [row])
        repo.replace_platform_holdings("toss", [row])

        holding = repo.holdings()[0]

        self.assertEqual(holding["alias"], "슈왑 미국 배당주 ETF")
        self.assertTrue(repo.delete_asset_alias("toss", "SCHD"))
        self.assertIsNone(repo.holdings()[0]["alias"])

    def test_strategy_can_be_deleted(self) -> None:
        repo = Repository(self.db_path)
        strategy = repo.create_strategy(
            {
                "name": "삭제 테스트",
                "strategy_type": "custom",
                "enabled": False,
                "platform": "",
                "symbol": "",
                "budget": 0,
                "params": {},
            }
        )

        self.assertTrue(repo.delete_strategy(strategy["id"]))
        self.assertEqual(repo.strategies(), [])
        self.assertFalse(repo.delete_strategy(strategy["id"]))

    def test_strategy_can_be_updated_without_changing_enabled_state(self) -> None:
        repo = Repository(self.db_path)
        strategy = repo.create_strategy(
            {
                "name": "수정 전",
                "strategy_type": "custom",
                "enabled": False,
                "platform": "",
                "symbol": "",
                "budget": 0,
                "params": {},
            }
        )
        repo.set_strategy_enabled(strategy["id"], True)

        updated = repo.update_strategy(
            strategy["id"],
            {
                "name": "수정 후",
                "strategy_type": "custom",
                "platform": "toss",
                "symbol": "SCHD",
                "budget": 100,
                "params": {},
            },
        )

        self.assertEqual(updated["name"], "수정 후")
        self.assertTrue(updated["enabled"])

    def test_existing_orders_table_gets_cost_estimate_columns(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_run_id INTEGER,
                    created_at TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    quantity REAL,
                    amount REAL,
                    currency TEXT NOT NULL DEFAULT 'KRW',
                    limit_price REAL,
                    dry_run INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    request_json TEXT NOT NULL
                )
                """
            )

        repo = Repository(self.db_path)

        with repo.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
        self.assertTrue(
            {
                "idempotency_key",
                "cancellation_policy",
                "reference_price",
                "estimated_notional",
                "estimated_fee",
                "estimated_tax",
                "estimated_slippage",
                "estimated_total",
            }.issubset(columns)
        )


class OperationalRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "dashboard.db"
        self.repo = Repository(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_schema_migration_history_is_recorded(self) -> None:
        self.assertEqual(self.repo.schema_version(), 2)
        self.assertEqual(
            [item["name"] for item in self.repo.migration_history()],
            ["baseline_existing_schema", "operational_locks_and_alerts"],
        )

    def test_platform_operation_lock_is_exclusive_and_releasable(self) -> None:
        self.assertEqual(
            self.repo.acquire_operation_lock("platform:upbit:operation", "owner-1"),
            "owner-1",
        )
        self.assertIsNone(self.repo.acquire_operation_lock("platform:upbit:operation", "owner-2"))
        self.assertEqual(len(self.repo.operation_locks()), 1)
        self.assertTrue(self.repo.release_operation_lock("platform:upbit:operation", "owner-1"))
        self.assertEqual(
            self.repo.acquire_operation_lock("platform:upbit:operation", "owner-2"),
            "owner-2",
        )

    def test_alerts_are_deduplicated_until_acknowledged(self) -> None:
        first = self.repo.record_alert(
            severity="error",
            category="sync_failure",
            platform="upbit",
            message="일시 장애",
            dedupe_key="sync-failure:upbit",
        )
        second = self.repo.record_alert(
            severity="error",
            category="sync_failure",
            platform="upbit",
            message="일시 장애 재발",
            dedupe_key="sync-failure:upbit",
        )

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["occurrences"], 2)
        self.assertEqual(self.repo.unresolved_alert_count(), 1)
        self.assertEqual(len(self.repo.alerts()), 1)
        acknowledged = self.repo.acknowledge_alert(first["id"])
        self.assertIsNotNone(acknowledged["acknowledged_at"])
        self.assertEqual(self.repo.unresolved_alert_count(), 0)

    def test_stale_running_sync_is_recovered_on_repository_start(self) -> None:
        sync_id = self.repo.start_sync("upbit")
        with self.repo.connect() as conn:
            conn.execute(
                "UPDATE sync_runs SET started_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", sync_id),
            )

        Repository(self.db_path)

        latest = self.repo.latest_sync_runs()[0]
        self.assertEqual(latest["status"], "failed")
        self.assertEqual(latest["error"], "프로세스 재시작 후 중단된 동기화입니다.")
        self.assertEqual(self.repo.alerts()[0]["category"], "stale_run_recovered")


class ExternalHTTPResilienceTests(unittest.TestCase):
    def test_transient_http_failure_retries_with_retry_after(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok": true}'
        response.headers = {}
        transient = HTTPError(
            "https://example.test",
            503,
            "temporarily unavailable",
            {"Retry-After": "0"},
            io.BytesIO(b"temporarily unavailable"),
        )
        opener = MagicMock(side_effect=[transient, response])
        sleeper = MagicMock()

        data, _ = request_json(
            Request("https://example.test", method="GET"),
            provider="test",
            opener=opener,
            policy=RetryPolicy(max_attempts=2, backoff_seconds=0, max_backoff_seconds=0, min_interval_seconds=0),
            limiter=RateLimiter(0),
            sleep_fn=sleeper,
        )

        self.assertEqual(data, {"ok": True})
        self.assertEqual(opener.call_count, 2)
        sleeper.assert_not_called()
        transient.close()

    def test_non_idempotent_request_is_not_retried_by_default(self) -> None:
        opener = MagicMock(side_effect=OSError("offline"))

        with self.assertRaises(RuntimeError):
            request_json(
                Request("https://example.test", data=b"{}", method="POST"),
                provider="test-post",
                opener=opener,
                policy=RetryPolicy(max_attempts=3, min_interval_seconds=0),
                limiter=RateLimiter(0),
                sleep_fn=MagicMock(),
            )

        self.assertEqual(opener.call_count, 1)


class AuthenticationTests(unittest.TestCase):
    def test_password_hash_is_not_reversible_and_sessions_have_csrf_tokens(self) -> None:
        encoded = hash_password("correct horse")
        self.assertTrue(verify_password("correct horse", encoded))
        self.assertFalse(verify_password("wrong horse", encoded))

        manager = AuthManager(
            enabled=True,
            viewer_password_hash=encoded,
            operator_password_hash=hash_password("operator password"),
        )
        session = manager.authenticate("viewer", "correct horse", client_key="test")

        self.assertEqual(session.user.role, "viewer")
        self.assertTrue(session.csrf_token)
        self.assertTrue(manager.csrf_valid({"X-CSRF-Token": session.csrf_token}, session))
        self.assertFalse(manager.csrf_valid({"X-CSRF-Token": "wrong"}, session))

    def test_failed_login_is_rate_limited(self) -> None:
        manager = AuthManager(
            enabled=True,
            viewer_password_hash=hash_password("viewer password"),
            operator_password_hash=hash_password("operator password"),
        )
        for _ in range(5):
            with self.assertRaises(PermissionError):
                manager.authenticate("viewer", "wrong", client_key="same-client")
        self.assertFalse(manager.login_allowed("same-client"))


class HTTPAuthenticationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.main import Handler

        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp_dir.name) / "dashboard.db")
        self.service = TradingService(self.repo)
        self.authentication = AuthManager(
            enabled=True,
            viewer_password_hash=hash_password("viewer password"),
            operator_password_hash=hash_password("operator password"),
        )
        self.handler_class = Handler

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict | list, dict[str, str]]:
        handler = self.handler_class.__new__(self.handler_class)
        handler.path = path
        handler.headers = headers or {}
        handler.server = SimpleNamespace(
            repository=self.repo,
            trading_service=self.service,
            authentication=self.authentication,
        )
        handler.wfile = io.BytesIO()
        response_status = {}
        response_headers: dict[str, str] = {}
        cookies: list[str] = []
        handler.send_response = lambda status: response_status.setdefault("status", status)

        def capture_header(name: str, value: str) -> None:
            if name.lower() == "set-cookie":
                cookies.append(value)
            else:
                response_headers[name] = value

        handler.send_header = capture_header
        handler.end_headers = lambda: None
        handler.read_json = lambda: payload or {}
        getattr(self.handler_class, f"do_{method}")(handler)
        response_headers["Set-Cookie"] = "\n".join(cookies)
        body = handler.wfile.getvalue().decode("utf-8")
        return response_status["status"], json.loads(body) if body else {}, response_headers

    @staticmethod
    def cookie_header(set_cookie: str) -> str:
        return "; ".join(item.split(";", 1)[0] for item in set_cookie.splitlines() if item)

    @staticmethod
    def csrf_token(cookie: str) -> str:
        for item in cookie.split("; "):
            if item.startswith("td_csrf="):
                return item.split("=", 1)[1]
        return ""

    def test_unauthenticated_reads_and_writes_are_blocked(self) -> None:
        status, _, _ = self.request("GET", "/api/strategies")
        self.assertEqual(status, 401)
        status, _, headers = self.request("GET", "/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login")
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_health_fails_when_authentication_is_incomplete(self) -> None:
        self.authentication = AuthManager(enabled=True)

        status, payload, _ = self.request("GET", "/api/health")

        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["auth"]["configured"])

    def test_operator_can_write_only_with_csrf_token_and_viewer_is_read_only(self) -> None:
        status, _, response_headers = self.request(
            "POST",
            "/api/auth/login",
            {"username": "operator", "password": "operator password"},
        )
        self.assertEqual(status, 200)
        operator_cookie = self.cookie_header(response_headers["Set-Cookie"])
        operator_csrf = self.csrf_token(operator_cookie)

        status, _, _ = self.request("POST", "/api/strategies", {}, headers={"Cookie": operator_cookie})
        self.assertEqual(status, 403)
        status, created, _ = self.request(
            "POST",
            "/api/strategies",
            {
                "name": "보호된 전략",
                "strategy_type": "custom",
                "platform": "",
                "params": {},
            },
            headers={"Cookie": operator_cookie, "X-CSRF-Token": operator_csrf},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["name"], "보호된 전략")

        status, _, viewer_headers = self.request(
            "POST",
            "/api/auth/login",
            {"username": "viewer", "password": "viewer password"},
        )
        viewer_cookie = self.cookie_header(viewer_headers["Set-Cookie"])
        viewer_csrf = self.csrf_token(viewer_cookie)
        status, _, _ = self.request(
            "POST",
            "/api/strategies",
            {"name": "차단 전략", "strategy_type": "custom", "platform": "", "params": {}},
            headers={"Cookie": viewer_cookie, "X-CSRF-Token": viewer_csrf},
        )
        self.assertEqual(status, 403)


class ApiKeyExpirationTests(unittest.TestCase):
    def test_expiration_statuses(self) -> None:
        values = {
            "UPBIT_KEY_EXPIRES_ON": "invalid-date",
            "TOSSINVEST_KEY_EXPIRES_ON": "2026-08-01",
            "KIS_PENSION_KEY_EXPIRES_ON": "2026-07-10",
            "KIS_ISA_KEY_EXPIRES_ON": "2026-06-01",
        }

        with patch.dict(os.environ, values):
            results = {item["platform"]: item for item in api_key_expirations(date(2026, 6, 27))}

        self.assertEqual(results["upbit"]["status"], "invalid")
        self.assertEqual(results["toss"]["status"], "valid")
        self.assertEqual(results["kis_pension"]["status"], "warning")
        self.assertEqual(results["kis_isa"]["status"], "expired")


class IntegrationClientTests(unittest.TestCase):
    def test_toss_closed_orders_follows_cursor_pagination(self) -> None:
        client = object.__new__(TossInvestClient)
        client.account_seq = "account-1"
        client._request = MagicMock(
            side_effect=[
                {
                    "result": {
                        "orders": [{"orderId": "1"}],
                        "hasNext": True,
                        "nextCursor": "next-page",
                    }
                },
                {
                    "result": {
                        "orders": [{"orderId": "2"}],
                        "hasNext": False,
                        "nextCursor": None,
                    }
                },
            ]
        )

        orders = client.closed_orders(from_date="2026-07-01", to_date="2026-07-30")

        self.assertEqual([item["orderId"] for item in orders], ["1", "2"])
        self.assertIn("cursor=next-page", client._request.call_args_list[1].args[1])

    def test_kis_domestic_executions_follows_continuation_keys(self) -> None:
        client = object.__new__(KISClient)
        client.account = SimpleNamespace(
            platform="kis_isa",
            account_no="12345678",
            product_code="01",
        )
        client.is_paper = False
        client._request = MagicMock(
            side_effect=[
                {
                    "rt_cd": "0",
                    "output1": [{"odno": "1"}],
                    "ctx_area_fk100": "fk",
                    "ctx_area_nk100": "nk",
                    "_response_headers": {"tr_cont": "M"},
                },
                {
                    "rt_cd": "0",
                    "output1": [{"odno": "2"}],
                    "ctx_area_fk100": "",
                    "ctx_area_nk100": "",
                    "_response_headers": {"tr_cont": ""},
                },
            ]
        )

        rows = client.domestic_executions(start_date="20260701", end_date="20260730")

        self.assertEqual([item["odno"] for item in rows], ["1", "2"])
        second_call = client._request.call_args_list[1]
        self.assertEqual(second_call.kwargs["params"]["CTX_AREA_FK100"], "fk")
        self.assertEqual(second_call.kwargs["params"]["CTX_AREA_NK100"], "nk")
        self.assertEqual(second_call.kwargs["tr_cont"], "N")

    def test_upbit_hashes_the_unencoded_query_string(self) -> None:
        client = object.__new__(UpbitClient)
        client.access_key = "access"
        client.secret_key = "secret"
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"[]"

        with patch("app.integrations.upbit.urlopen", return_value=response) as request:
            client.closed_orders(
                start_time="2026-07-01T00:00:00Z",
                end_time="2026-07-08T00:00:00Z",
            )

        sent_request = request.call_args.args[0]
        query = sent_request.full_url.split("?", 1)[1]
        token = sent_request.headers["Authorization"].split(" ", 1)[1]
        payload_part = token.split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4))
        )

        self.assertNotIn("%3A", query)
        self.assertEqual(
            payload["query_hash"],
            hashlib.sha512(query.encode("utf-8")).hexdigest(),
        )


class ValidationTests(unittest.TestCase):
    def test_strategy_is_normalized(self) -> None:
        result = validate_strategy(
            {
                "name": "  월간 리밸런싱  ",
                "strategy_type": "rebalance",
                "budget": "1000",
                "enabled": True,
            }
        )

        self.assertEqual(result["name"], "월간 리밸런싱")
        self.assertEqual(result["budget"], 1000)
        self.assertFalse(result["enabled"])

    def test_strategy_rejects_invalid_numbers(self) -> None:
        for budget in [-1, "nan", "not-a-number"]:
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                validate_strategy({"name": "테스트", "budget": budget})

    def test_dca_normalizes_asset_specific_amounts(self) -> None:
        result = validate_strategy(
            {
                "name": "매일 1달러",
                "strategy_type": "dca",
                "platform": "toss",
                "params": {
                    "items": [
                        {"symbol": " schd ", "market": "overseas", "value": "1"},
                        {"symbol": "VT", "market": "overseas", "value": "2"},
                    ],
                    "interval": "daily",
                    "execution_time": "22:30",
                },
            }
        )

        self.assertEqual(result["symbol"], "SCHD,VT")
        self.assertEqual(
            result["params"],
            {
                "items": [
                    {"symbol": "SCHD", "order_type": "amount", "amount": 1, "currency": "USD", "market": "overseas"},
                    {"symbol": "VT", "order_type": "amount", "amount": 2, "currency": "USD", "market": "overseas"},
                ],
                "interval": "daily",
                "execution_time": "22:30",
                "cost_overrides": {"fee_pct": None, "tax_pct": None, "slippage_pct": 0.0},
                "risk_limits": {"daily_budget_krw": 0.0, "max_orders_per_day": 20},
            },
        )

    def test_dca_rejects_duplicate_symbols(self) -> None:
        with self.assertRaisesRegex(ValueError, "중복"):
            validate_strategy(
                {
                    "name": "중복 DCA",
                    "strategy_type": "dca",
                    "platform": "toss",
                    "params": {
                        "items": [
                            {"symbol": "SCHD", "value": 1},
                            {"symbol": "schd", "value": 2},
                        ]
                    },
                }
            )

    def test_dca_requires_platform_and_symbols(self) -> None:
        for platform, symbol in [("", "SCHD"), ("toss", "")]:
            with self.subTest(platform=platform, symbol=symbol), self.assertRaises(ValueError):
                validate_strategy(
                    {
                        "name": "DCA",
                        "strategy_type": "dca",
                        "platform": platform,
                        "symbol": symbol,
                    }
                )

    def test_dca_supports_other_platforms(self) -> None:
        result = validate_strategy(
            {
                "name": "한투 DCA",
                "strategy_type": "dca",
                "platform": "kis_isa",
                "symbol": "458730",
            }
        )

        self.assertEqual(result["platform"], "kis_isa")
        self.assertEqual(
            result["params"]["items"],
            [{"symbol": "458730", "market": "domestic", "order_type": "quantity", "currency": "KRW", "quantity": 1}],
        )

    def test_domestic_dca_requires_integer_quantity(self) -> None:
        with self.assertRaisesRegex(ValueError, "정수"):
            validate_strategy(
                {
                    "name": "국내 DCA",
                    "strategy_type": "dca",
                    "platform": "kis_isa",
                    "params": {"items": [{"symbol": "458730", "value": 1.5}]},
                }
            )

    def test_toss_supports_domestic_quantity_and_overseas_amount(self) -> None:
        result = validate_strategy(
            {
                "name": "토스 혼합 DCA",
                "strategy_type": "dca",
                "platform": "toss",
                "params": {
                    "items": [
                        {"symbol": "005930", "market": "domestic", "value": 2},
                        {"symbol": "SCHD", "market": "overseas", "value": 1},
                    ]
                },
            }
        )

        self.assertEqual(
            result["params"]["items"],
            [
                {"symbol": "005930", "market": "domestic", "order_type": "quantity", "currency": "KRW", "quantity": 2},
                {"symbol": "SCHD", "market": "overseas", "order_type": "amount", "currency": "USD", "amount": 1},
            ],
        )

    def test_upbit_dca_uses_krw_amount(self) -> None:
        result = validate_strategy(
            {
                "name": "업비트 DCA",
                "strategy_type": "dca",
                "platform": "upbit",
                "params": {"items": [{"symbol": "KRW-BTC", "value": 5000}]},
            }
        )

        self.assertEqual(
            result["params"]["items"],
            [{"symbol": "KRW-BTC", "market": "crypto", "order_type": "amount", "currency": "KRW", "amount": 5000}],
        )

    def test_dca_normalizes_and_validates_cost_overrides(self) -> None:
        result = validate_strategy(
            {
                "name": "비용 반영 DCA",
                "strategy_type": "dca",
                "platform": "upbit",
                "params": {
                    "items": [{"symbol": "KRW-BTC", "value": 5000}],
                    "cost_overrides": {
                        "fee_pct": "0.05",
                        "tax_pct": 0,
                        "slippage_pct": "0.1",
                    },
                },
            }
        )

        self.assertEqual(
            result["params"]["cost_overrides"],
            {"fee_pct": 0.05, "tax_pct": 0.0, "slippage_pct": 0.1},
        )
        for invalid_rate in (-0.1, 100.1, "nan", "invalid"):
            with self.subTest(invalid_rate=invalid_rate), self.assertRaisesRegex(ValueError, "거래 비용률"):
                validate_strategy(
                    {
                        "name": "잘못된 비용률",
                        "strategy_type": "dca",
                        "platform": "upbit",
                        "params": {
                            "items": [{"symbol": "KRW-BTC", "value": 5000}],
                            "cost_overrides": {"fee_pct": invalid_rate},
                        },
                    }
                )
        with self.assertRaisesRegex(ValueError, "객체 형식"):
            validate_strategy(
                {
                    "name": "잘못된 비용률 형식",
                    "strategy_type": "dca",
                    "platform": "upbit",
                    "params": {
                        "items": [{"symbol": "KRW-BTC", "value": 5000}],
                        "cost_overrides": [],
                    },
                }
            )


class OrderCostTests(unittest.TestCase):
    def test_estimates_amount_order_cost(self) -> None:
        estimate = estimate_dca_buy_cost(
            {"order_type": "amount", "amount": 5000},
            {"fee_pct": 0.05, "tax_pct": 0, "slippage_pct": 0.1},
        )

        self.assertEqual(estimate["estimated_notional"], 5000)
        self.assertEqual(estimate["estimated_fee"], 2.5)
        self.assertEqual(estimate["estimated_tax"], 0)
        self.assertEqual(estimate["estimated_slippage"], 5)
        self.assertEqual(estimate["estimated_total"], 5007.5)

    def test_quantity_order_requires_reference_price(self) -> None:
        missing = estimate_dca_buy_cost(
            {"order_type": "quantity", "quantity": 2},
            {"fee_pct": 0.1, "tax_pct": 0, "slippage_pct": 0},
        )
        estimated = estimate_dca_buy_cost(
            {"order_type": "quantity", "quantity": 2},
            {"fee_pct": 0.1, "tax_pct": 0, "slippage_pct": 0},
            reference_price=10_000,
        )

        self.assertIsNone(missing["estimated_total"])
        self.assertEqual(estimated["estimated_notional"], 20_000)
        self.assertEqual(estimated["estimated_total"], 20_020)


class FeePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FeePolicyStore()

    def test_applies_official_platform_defaults_and_toss_waiver(self) -> None:
        upbit = self.store.resolve_cost_profile(
            "upbit",
            {"market": "crypto"},
            notional=5000,
        )
        toss_waived = self.store.resolve_cost_profile(
            "toss",
            {"market": "overseas"},
            notional=10,
        )
        toss_standard = self.store.resolve_cost_profile(
            "toss",
            {"market": "overseas"},
            notional=10.01,
        )

        self.assertEqual(upbit["fee_pct"], 0.05)
        self.assertEqual(upbit["fee_source"]["kind"], "official_policy")
        self.assertEqual(toss_waived["fee_pct"], 0)
        self.assertEqual(toss_standard["fee_pct"], 0.1)

    def test_live_fee_takes_priority_over_user_override(self) -> None:
        profile = self.store.resolve_cost_profile(
            "upbit",
            {"market": "crypto"},
            notional=5000,
            cost_overrides={"fee_pct": 0.2},
            live_fee={
                "fee_pct": 0.03,
                "label": "테스트 실시간 요율",
                "checked_at": "2026-07-30T00:00:00+00:00",
            },
        )

        self.assertEqual(profile["fee_pct"], 0.03)
        self.assertEqual(profile["fee_source"]["kind"], "live_api")

    def test_policy_file_changes_apply_on_next_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "fees.json"
            policy = {
                "schema_version": 1,
                "policy_version": "test-1",
                "platforms": {
                    "upbit": {
                        "markets": {
                            "crypto": {
                                "fee_pct": 0.07,
                                "buy_tax_pct": 0,
                                "source": {"label": "테스트 정책"},
                            }
                        }
                    }
                },
            }
            path.write_text(json.dumps(policy), encoding="utf-8")
            store = FeePolicyStore(path)

            first = store.resolve_cost_profile("upbit", {"market": "crypto"}, notional=5000)
            policy["platforms"]["upbit"]["markets"]["crypto"]["fee_pct"] = 0.08
            path.write_text(json.dumps(policy), encoding="utf-8")
            second = store.resolve_cost_profile("upbit", {"market": "crypto"}, notional=5000)

        self.assertEqual(first["fee_pct"], 0.07)
        self.assertEqual(second["fee_pct"], 0.08)


class ExecutionNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp_dir.name) / "dashboard.db")
        self.service = TradingService(self.repo, upbit_fee_provider=lambda _: None)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_toss_uses_actual_commission_and_tax(self) -> None:
        execution = self.service._normalize_toss_execution(
            {
                "orderId": "toss-order",
                "symbol": "005930",
                "side": "BUY",
                "orderType": "MARKET",
                "status": "FILLED",
                "currency": "KRW",
                "orderedAt": "2026-03-28T09:30:00+09:00",
                "execution": {
                    "filledQuantity": "10",
                    "averageFilledPrice": "70000",
                    "filledAmount": "700000",
                    "commission": "1400",
                    "tax": "0",
                    "filledAt": "2026-03-28T09:31:15+09:00",
                },
            }
        )

        self.assertEqual(execution["actual_fee"], 1400)
        self.assertIsNone(execution["estimated_fee"])
        self.assertEqual(execution["actual_tax"], 0)
        self.assertEqual(execution["cost_profile"]["fee_source"]["kind"], "actual_api")

    def test_toss_falls_back_to_official_policy_when_commission_is_missing(self) -> None:
        execution = self.service._normalize_toss_execution(
            {
                "orderId": "toss-order",
                "symbol": "005930",
                "side": "BUY",
                "orderType": "MARKET",
                "status": "FILLED",
                "currency": "KRW",
                "orderedAt": "2026-03-28T09:30:00+09:00",
                "execution": {
                    "filledQuantity": "10",
                    "averageFilledPrice": "70000",
                    "filledAmount": "700000",
                    "commission": None,
                    "tax": None,
                    "filledAt": "2026-03-28T09:31:15+09:00",
                },
            }
        )

        self.assertAlmostEqual(execution["estimated_fee"], 105)
        self.assertEqual(execution["cost_profile"]["fee_source"]["kind"], "official_policy")

    def test_upbit_uses_paid_fee(self) -> None:
        execution = _normalize_upbit_execution(
            {
                "uuid": "upbit-order",
                "market": "KRW-BTC",
                "side": "bid",
                "ord_type": "price",
                "state": "done",
                "created_at": "2026-07-30T09:00:00+09:00",
                "executed_volume": "0.001",
                "executed_funds": "100000",
                "paid_fee": "50",
            },
            {"KRW-BTC": "비트코인"},
            FeePolicyStore(),
        )

        self.assertEqual(execution["actual_fee"], 50)
        self.assertIsNone(execution["estimated_fee"])
        self.assertEqual(execution["cost_profile"]["fee_source"]["kind"], "actual_api")

    def test_kis_marks_fee_as_official_policy_estimate(self) -> None:
        execution = self.service._normalize_kis_execution(
            "kis_isa",
            {
                "ord_dt": "20260730",
                "ord_tmd": "093000",
                "ord_gno_brno": "12345",
                "odno": "000001",
                "pdno": "005930",
                "prdt_name": "삼성전자",
                "sll_buy_dvsn_cd": "02",
                "ord_dvsn_name": "시장가",
                "tot_ccld_qty": "10",
                "avg_prvs": "70000",
                "tot_ccld_amt": "700000",
                "rmn_qty": "0",
            },
            {"005930": "stock"},
        )

        self.assertIsNone(execution["actual_fee"])
        self.assertAlmostEqual(execution["estimated_fee"], 98.3689)
        self.assertEqual(execution["cost_profile"]["fee_source"]["kind"], "official_policy")


class StrategyCapabilityTests(unittest.TestCase):
    def test_capabilities_describe_platform_specific_inputs(self) -> None:
        platforms = strategy_capabilities()["platforms"]

        self.assertEqual(platforms["toss"]["markets"]["overseas"]["order_mode"], "amount")
        self.assertEqual(platforms["toss"]["markets"]["domestic"]["order_mode"], "quantity")
        self.assertEqual(platforms["kis_isa"]["markets"]["domestic"]["integer_only"], True)
        self.assertEqual(platforms["upbit"]["markets"]["crypto"]["value_min"], 5000)

    def test_compiles_toss_amount_order(self) -> None:
        request = compile_dca_buy_request(
            "toss",
            {"symbol": "SCHD", "market": "overseas", "order_type": "amount", "amount": 1, "currency": "USD"},
        )

        self.assertEqual(
            request["body"],
            {"symbol": "SCHD", "side": "BUY", "orderType": "MARKET", "orderAmount": "1"},
        )

    def test_compiles_kis_domestic_market_order(self) -> None:
        request = compile_dca_buy_request(
            "kis_isa",
            {"symbol": "458730", "market": "domestic", "order_type": "quantity", "quantity": 2, "currency": "KRW"},
        )

        self.assertEqual(request["body"]["ORD_DVSN"], "01")
        self.assertEqual(request["body"]["ORD_QTY"], "2")
        self.assertEqual(request["body"]["ORD_UNPR"], "0")
        self.assertEqual(request["body"]["EXCG_ID_DVSN_CD"], "KRX")

    def test_compiles_upbit_market_buy(self) -> None:
        request = compile_dca_buy_request(
            "upbit",
            {"symbol": "KRW-BTC", "market": "crypto", "order_type": "amount", "amount": 5000, "currency": "KRW"},
        )

        self.assertEqual(
            request["body"],
            {"market": "KRW-BTC", "side": "bid", "ord_type": "price", "price": "5000"},
        )

    def test_dca_rejects_invalid_execution_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            validate_strategy(
                {
                    "name": "DCA",
                    "strategy_type": "dca",
                    "platform": "toss",
                    "symbol": "SCHD",
                    "params": {"execution_time": "25:00"},
                }
            )

    def test_dca_validates_weekly_and_monthly_execution_days(self) -> None:
        weekly = validate_strategy(
            {
                "name": "주간 DCA",
                "strategy_type": "dca",
                "platform": "upbit",
                "params": {"items": [{"symbol": "KRW-BTC", "value": 5000}], "interval": "weekly", "execution_day": "friday"},
            }
        )
        monthly = validate_strategy(
            {
                "name": "월간 DCA",
                "strategy_type": "dca",
                "platform": "upbit",
                "params": {"items": [{"symbol": "KRW-BTC", "value": 5000}], "interval": "monthly", "execution_day": 15},
            }
        )

        self.assertEqual(weekly["params"]["execution_day"], "friday")
        self.assertEqual(monthly["params"]["execution_day"], 15)
        with self.assertRaisesRegex(ValueError, "1일부터 28일까지"):
            validate_strategy(
                {
                    "name": "잘못된 월간 DCA",
                    "strategy_type": "dca",
                    "platform": "upbit",
                    "params": {"items": [{"symbol": "KRW-BTC", "value": 5000}], "interval": "monthly", "execution_day": 31},
                }
            )


class StrategyRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp_dir.name) / "dashboard.db")
        self.upbit_chance = {
            "market": {
                "id": "KRW-BTC",
                "state": "active",
                "order_types": ["price"],
                "order_sides": ["bid"],
                "min_total": "5000",
                "max_total": "100000000",
            },
            "bid_fee": "0.0005",
            "bid_account": {"balance": "100000"},
        }
        self.service = TradingService(
            self.repo,
            upbit_fee_provider=lambda _: None,
            upbit_preflight_provider=lambda _: self.upbit_chance,
            clock=lambda: datetime(2026, 8, 4, 13, 0, tzinfo=KST),
        )
        self.repo.replace_platform_holdings(
            "upbit",
            [
                {
                    "symbol": "KRW",
                    "name": "원화",
                    "asset_type": "cash",
                    "quantity": 100_000,
                    "avg_price": 1,
                    "current_price": 1,
                    "currency": "KRW",
                }
            ],
        )
        self.strategy = self.repo.create_strategy(
            validate_strategy(
                {
                    "name": "BTC 매일 매수",
                    "strategy_type": "dca",
                    "platform": "upbit",
                    "params": {
                        "items": [{"symbol": "KRW-BTC", "value": 5000}],
                        "interval": "daily",
                        "execution_time": "09:30",
                        "cost_overrides": {
                            "slippage_pct": 0.1,
                        },
                    },
                }
            )
        )
        self.repo.set_strategy_enabled(self.strategy["id"], True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_manual_dry_run_records_run_and_order(self) -> None:
        run = self.service.run_dca_strategy_now(self.strategy["id"])

        self.assertEqual(run["status"], "success")
        self.assertEqual(run["order_count"], 1)
        history = self.repo.strategy_runs()
        self.assertEqual(history[0]["trigger"], "manual")
        self.assertEqual(history[0]["orders"][0]["status"], "dry_run")
        self.assertEqual(history[0]["orders"][0]["amount"], 5000)
        self.assertEqual(history[0]["orders"][0]["estimated_fee"], 2.5)
        self.assertEqual(history[0]["orders"][0]["estimated_slippage"], 5)
        self.assertEqual(history[0]["orders"][0]["estimated_total"], 5007.5)
        self.assertEqual(
            history[0]["orders"][0]["cost_profile"]["fee_source"]["kind"],
            "official_policy",
        )
        self.assertEqual(history[0]["orders"][0]["cancellation_policy"], "reject_before_submission")
        self.assertLessEqual(len(history[0]["orders"][0]["idempotency_key"]), 36)
        request = json.loads(history[0]["orders"][0]["request_json"])
        self.assertEqual(request["idempotency_key"], history[0]["orders"][0]["idempotency_key"])

    def test_strategy_execution_is_skipped_when_platform_is_locked(self) -> None:
        self.repo.acquire_operation_lock("platform:upbit:operation", "another-worker")

        result = self.service.run_dca_strategy_now(self.strategy["id"])

        self.assertEqual(result["status"], "busy")
        self.assertEqual(self.repo.strategy_runs(), [])
        self.assertEqual(self.repo.alerts()[0]["category"], "operation_lock")

    def test_preflight_failure_is_recorded_without_a_dry_run_order(self) -> None:
        preflight = MagicMock(return_value={"error": "종목 조회 실패"})
        service = TradingService(
            self.repo,
            upbit_preflight_provider=preflight,
            clock=lambda: datetime(2026, 8, 4, 13, 0, tzinfo=KST),
        )

        run = service.run_dca_strategy_now(self.strategy["id"])

        self.assertEqual(run["status"], "failed")
        self.assertIn("주문 전 검증 실패", run["error"])
        self.assertEqual(preflight.call_count, 1)
        orders = self.repo.orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "risk_rejected")
        self.assertIn("종목 조회 실패", orders[0]["reason"])
        self.assertEqual(self.repo.strategy_runs()[0]["orders"][0]["status"], "risk_rejected")

    def test_daily_order_limit_rejects_the_second_run(self) -> None:
        strategy = self.repo.create_strategy(
            validate_strategy(
                {
                    "name": "일일 한도 DCA",
                    "strategy_type": "dca",
                    "platform": "upbit",
                    "budget": 6_000,
                    "params": {
                        "items": [{"symbol": "KRW-BTC", "value": 5000}],
                        "max_orders_per_day": 1,
                    },
                }
            )
        )

        first = self.service.run_dca_strategy_now(strategy["id"])
        second = self.service.run_dca_strategy_now(strategy["id"])

        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "failed")
        self.assertIn("일일 최대 주문 횟수", second["error"])
        self.assertEqual(len(self.repo.orders()), 2)
        self.assertEqual(self.repo.orders()[0]["status"], "risk_rejected")

    def test_upbit_live_fee_overrides_policy_default(self) -> None:
        service = TradingService(
            self.repo,
            upbit_fee_provider=lambda _: {
                "fee_pct": 0.03,
                "label": "업비트 테스트 조회",
                "checked_at": "2026-07-30T00:00:00+00:00",
            },
            upbit_preflight_provider=lambda _: self.upbit_chance,
            clock=lambda: datetime(2026, 8, 4, 13, 0, tzinfo=KST),
        )

        service.run_dca_strategy_now(self.strategy["id"])

        order = self.repo.orders()[0]
        self.assertEqual(order["cost_profile"]["fee_pct"], 0.03)
        self.assertEqual(order["cost_profile"]["fee_source"]["kind"], "live_api")
        self.assertEqual(order["estimated_fee"], 1.5)

    def test_upbit_fee_lookup_failure_uses_policy_default(self) -> None:
        service = TradingService(
            self.repo,
            upbit_fee_provider=lambda _: {"error": "offline"},
            upbit_preflight_provider=lambda _: self.upbit_chance,
            clock=lambda: datetime(2026, 8, 4, 13, 0, tzinfo=KST),
        )

        service.run_dca_strategy_now(self.strategy["id"])

        order = self.repo.orders()[0]
        self.assertEqual(order["cost_profile"]["fee_pct"], 0.05)
        self.assertEqual(order["cost_profile"]["fee_source"]["kind"], "official_policy")
        self.assertEqual(order["cost_profile"]["live_fee_lookup"]["status"], "fallback")

    def test_quantity_order_uses_latest_holding_price_for_cost_estimate(self) -> None:
        self.repo.replace_platform_holdings(
            "kis_isa",
            [
                {
                    "symbol": "458730",
                    "name": "KODEX 미국S&P500",
                    "asset_type": "etf",
                    "quantity": 1,
                    "avg_price": 10_000,
                    "current_price": 12_000,
                    "currency": "KRW",
                },
                {
                    "symbol": "KRW",
                    "name": "주문 가능 현금",
                    "asset_type": "cash",
                    "quantity": 100_000,
                    "avg_price": 1,
                    "current_price": 1,
                    "currency": "KRW",
                },
            ],
        )
        strategy = self.repo.create_strategy(
            validate_strategy(
                {
                    "name": "국내 ETF DCA",
                    "strategy_type": "dca",
                    "platform": "kis_isa",
                    "params": {
                        "items": [{"symbol": "458730", "value": 2}],
                    },
                }
            )
        )

        self.service.run_dca_strategy_now(strategy["id"])

        order = self.repo.orders()[0]
        self.assertEqual(order["reference_price"], 12_000)
        self.assertEqual(order["estimated_notional"], 24_000)
        self.assertEqual(order["cost_profile"]["fee_pct"], 0.0146527)
        self.assertEqual(order["estimated_fee"], 3.516648)
        self.assertEqual(order["estimated_total"], 24_003.516648)

    def test_due_run_executes_once_per_scheduled_minute(self) -> None:
        now = datetime(2026, 7, 12, 9, 30, tzinfo=KST)

        first = self.service.run_due_dca_strategies(now)
        second = self.service.run_due_dca_strategies(now)

        self.assertEqual(len(first["runs"]), 1)
        self.assertEqual(second["runs"], [])
        self.assertEqual(len(self.repo.strategy_runs()), 1)
        self.assertEqual(len(self.repo.orders()), 1)

    def test_different_strategies_can_use_the_same_scheduled_minute(self) -> None:
        second = self.repo.create_strategy(
            validate_strategy(
                {
                    "name": "BTC 두 번째 전략",
                    "strategy_type": "dca",
                    "platform": "upbit",
                    "params": {"items": [{"symbol": "KRW-BTC", "value": 5000}], "execution_time": "09:30"},
                }
            )
        )
        self.repo.set_strategy_enabled(second["id"], True)

        result = self.service.run_due_dca_strategies(datetime(2026, 7, 12, 9, 30, tzinfo=KST))

        self.assertEqual(len(result["runs"]), 2)
        self.assertEqual(len(self.repo.strategy_runs()), 2)

    def test_weekly_and_monthly_schedule_slots(self) -> None:
        strategy = self.repo.strategy(self.strategy["id"])
        strategy["params"].update({"interval": "weekly", "execution_day": "sunday"})
        sunday = datetime(2026, 7, 12, 9, 30, tzinfo=KST)
        monday = datetime(2026, 7, 13, 9, 30, tzinfo=KST)

        self.assertIsNotNone(scheduled_slot(strategy, sunday))
        self.assertIsNone(scheduled_slot(strategy, monday))
        strategy["params"].update({"interval": "monthly", "execution_day": 12})
        self.assertIsNotNone(scheduled_slot(strategy, sunday))

    def test_run_history_survives_strategy_deletion(self) -> None:
        self.service.run_dca_strategy_now(self.strategy["id"])
        self.repo.delete_strategy(self.strategy["id"])

        history = self.repo.strategy_runs()

        self.assertEqual(history[0]["strategy_name"], "삭제된 전략")
        self.assertEqual(len(history[0]["orders"]), 1)


class ExchangeRateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp_dir.name) / "dashboard.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_uses_last_successful_rate_when_fetch_fails(self) -> None:
        self.repo.record_exchange_rate(
            pair="USD/KRW",
            rate=1350,
            source="test",
            status="success",
        )

        with (
            patch.dict(os.environ, {"USD_KRW_RATE": ""}),
            patch("app.integrations.fx.urlopen", side_effect=OSError("offline")),
        ):
            result = usd_krw_rate(self.repo)

        self.assertEqual(result["rate"], 1350)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["source"], "cached")

    def test_prefers_toss_rate(self) -> None:
        client = SimpleNamespace(
            exchange_rate=lambda base, quote: {
                "result": {
                    "baseCurrency": base,
                    "quoteCurrency": quote,
                    "rate": "1380.5",
                    "midRate": "1375",
                    "basisPoint": "40",
                    "validUntil": "2026-03-25T09:31:00+09:00",
                }
            }
        )

        with (
            patch.dict(os.environ, {"USD_KRW_RATE": ""}),
            patch("app.integrations.fx.urlopen") as er_api,
        ):
            result = usd_krw_rate(self.repo, client)

        self.assertEqual(result["rate"], 1380.5)
        self.assertEqual(result["source"], "tossinvest")
        self.assertEqual(result["details"]["mid_rate"], 1375)
        self.assertEqual(result["details"]["basis_point"], 40)
        er_api.assert_not_called()

    def test_falls_back_to_er_api_when_toss_fetch_fails(self) -> None:
        client = SimpleNamespace(exchange_rate=lambda base, quote: (_ for _ in ()).throw(RuntimeError("offline")))
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"rates":{"KRW":1375.25}}'

        with (
            patch.dict(os.environ, {"USD_KRW_RATE": ""}),
            patch("app.integrations.fx.urlopen", return_value=response),
        ):
            result = usd_krw_rate(self.repo, client)

        self.assertEqual(result["rate"], 1375.25)
        self.assertEqual(result["source"], "open.er-api.com")

    def test_fails_without_a_cached_rate(self) -> None:
        with (
            patch.dict(os.environ, {"USD_KRW_RATE": ""}),
            patch("app.integrations.fx.urlopen", side_effect=OSError("offline")),
            self.assertRaises(FxError),
        ):
            usd_krw_rate(self.repo)

        self.assertEqual(self.repo.latest_exchange_rate()["status"], "failed")


class SyncIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("TRADING_DASHBOARD_SEED_DEMO", None)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp_dir.name) / "dashboard.db")
        self.service = TradingService(self.repo)
        self.repo.replace_platform_holdings(
            "upbit",
            [
                {
                    "symbol": "KRW-BTC",
                    "name": "비트코인",
                    "asset_type": "crypto",
                    "quantity": 1,
                    "avg_price": 100,
                    "current_price": 110,
                    "currency": "KRW",
                }
            ],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_failed_sync_preserves_existing_holdings(self) -> None:
        def fail() -> dict:
            raise RuntimeError("temporary failure")

        result = self.service._run_sync("upbit", fail)

        self.assertFalse(result["ok"])
        self.assertEqual(self.repo.holdings()[0]["symbol"], "KRW-BTC")
        latest = self.repo.latest_sync_runs()[0]
        self.assertEqual(latest["status"], "failed")
        self.assertEqual(latest["error"], "temporary failure")
        self.assertEqual(self.repo.alerts()[0]["category"], "sync_failure")

    def test_sync_is_skipped_when_platform_operation_is_locked(self) -> None:
        self.repo.acquire_operation_lock("platform:upbit:operation", "another-worker")

        result = self.service._run_sync("upbit", lambda: {"synced_count": 1})

        self.assertEqual(result["status"], "busy")
        self.assertEqual(self.repo.recent_sync_runs(), [])
        self.assertEqual(self.repo.alerts()[0]["category"], "operation_lock")

    def test_kis_accounts_sync_independently(self) -> None:
        accounts = [
            SimpleNamespace(platform="kis_pension"),
            SimpleNamespace(platform="kis_isa"),
        ]

        def sync_account(account) -> dict:
            if account.platform == "kis_pension":
                raise RuntimeError("pension unavailable")
            return {"platform": account.platform, "synced_count": 0, "holdings": []}

        with (
            patch("app.services.kis_accounts", return_value=accounts),
            patch.object(self.service, "_sync_kis_account", side_effect=sync_account),
        ):
            result = self.service.sync_kis_holdings()

        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["ok"])
        self.assertEqual([item["status"] for item in result["results"]], ["failed", "success"])


class MaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "dashboard.db"
        self.repo = Repository(self.db_path)
        self.repo.create_strategy(
            {
                "name": "백업 테스트",
                "strategy_type": "custom",
                "enabled": False,
                "platform": "",
                "symbol": "",
                "budget": 0,
                "params": {},
            }
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_backup_restore_and_integrity_check(self) -> None:
        from app.maintenance import backup_database, integrity_check, restore_database

        backup_path = self.root / "backups" / "dashboard-backup.db"
        restored_path = self.root / "restored.db"
        backup_result = backup_database(self.db_path, backup_path)
        restore_result = restore_database(backup_path, restored_path)

        self.assertTrue(backup_result["integrity"]["ok"])
        self.assertTrue(restore_result["integrity"]["ok"])
        self.assertTrue(integrity_check(restored_path)["ok"])
        self.assertEqual(len(Repository(restored_path).strategies()), 1)


class HTTPApiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.main import Handler

        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp_dir.name) / "dashboard.db")
        self.service = TradingService(self.repo)
        self.handler_class = Handler

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict | list]:
        handler = self.handler_class.__new__(self.handler_class)
        handler.path = path
        handler.server = SimpleNamespace(repository=self.repo, trading_service=self.service)
        handler.wfile = io.BytesIO()
        response_status = {}
        handler.send_response = lambda status: response_status.setdefault("status", status)
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.read_json = lambda: payload or {}
        getattr(self.handler_class, f"do_{method}")(handler)
        return response_status["status"], json.loads(handler.wfile.getvalue().decode("utf-8"))

    def test_health_strategy_and_alert_endpoints(self) -> None:
        status, health = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["mode"], "dry_run")
        self.assertEqual(health["active_alerts"], 0)

        status, created = self.request(
            "POST",
            "/api/strategies",
            {
                "name": "HTTP DCA",
                "strategy_type": "dca",
                "platform": "upbit",
                "params": {"items": [{"symbol": "KRW-BTC", "value": 5000}]},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["strategy_type"], "dca")

        self.repo.record_alert(
            severity="error",
            category="test",
            message="테스트 알림",
            dedupe_key="test-alert",
        )
        status, alerts = self.request("GET", "/api/alerts")
        self.assertEqual(status, 200)
        self.assertEqual(alerts[0]["message"], "테스트 알림")

        status, acknowledged = self.request("PATCH", f"/api/alerts/{alerts[0]['id']}?acknowledged=true")
        self.assertEqual(status, 200)
        self.assertIsNotNone(acknowledged["acknowledged_at"])


if __name__ == "__main__":
    unittest.main()
