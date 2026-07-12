from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import api_key_expirations
from app.integrations.fx import FxError, usd_krw_rate
from app.repository import Repository
from app.scheduler import KST, scheduled_slot
from app.services import TradingService
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

        run = repo.finish_sync(sync_id, status="success", synced_count=3)

        self.assertEqual(run["status"], "success")
        self.assertEqual(run["synced_count"], 3)
        self.assertEqual(repo.latest_sync_runs()[0]["platform"], "upbit")
        self.assertEqual(repo.recent_sync_runs()[0]["id"], sync_id)

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
        self.service = TradingService(self.repo)
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

    def test_due_run_executes_once_per_scheduled_minute(self) -> None:
        now = datetime(2026, 7, 12, 9, 30, tzinfo=KST)

        first = self.service.run_due_dca_strategies(now)
        second = self.service.run_due_dca_strategies(now)

        self.assertEqual(len(first["runs"]), 1)
        self.assertEqual(second["runs"], [])
        self.assertEqual(len(self.repo.strategy_runs()), 1)
        self.assertEqual(len(self.repo.orders()), 1)

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


if __name__ == "__main__":
    unittest.main()
