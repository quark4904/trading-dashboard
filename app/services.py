from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Callable

from app.config import platform_configs
from app.integrations.fx import usd_krw_rate
from app.integrations.kis import KISClient, kis_accounts
from app.integrations.tossinvest import TossInvestClient
from app.integrations.upbit import UpbitClient
from app.order_costs import estimate_dca_buy_cost, normalize_cost_assumptions
from app.repository import Repository
from app.scheduler import KST, scheduled_slot
from app.strategy_capabilities import compile_dca_buy_request


DUST_VALUE_THRESHOLD = 100


class TradingService:
    def __init__(self, repo: Repository):
        self.repo = repo

    def platforms(self) -> list[dict[str, Any]]:
        configs = platform_configs()
        holdings = self.repo.holdings()
        invested_by_platform = defaultdict(float)
        value_by_platform = defaultdict(float)
        for item in holdings:
            invested_by_platform[item["platform"]] += item["quantity"] * item["avg_price"]
            value_by_platform[item["platform"]] += item["quantity"] * item["current_price"]

        return [
            {
                "code": config.code,
                "name": config.name,
                "category": config.category,
                "configured": config.configured,
                "live_trading": False,
                "market_value": value_by_platform[config.code],
                "pnl": value_by_platform[config.code] - invested_by_platform[config.code],
            }
            for config in configs
        ]

    def portfolio_summary(self) -> dict[str, Any]:
        holdings = self.repo.holdings()
        total_cost = 0.0
        total_value = 0.0
        by_platform: dict[str, dict[str, Any]] = {}
        by_symbol: list[dict[str, Any]] = []
        tradable_symbols: list[dict[str, Any]] = []
        small_symbols: list[dict[str, Any]] = []
        cash_by_platform: dict[str, dict[str, Any]] = {}

        platform_names = {item["code"]: item["name"] for item in self.platforms()}
        for item in holdings:
            cost = item["quantity"] * item["avg_price"]
            value = item["quantity"] * item["current_price"]
            pnl = value - cost
            pnl_pct = (pnl / cost * 100) if cost else 0
            total_cost += cost
            total_value += value

            platform = by_platform.setdefault(
                item["platform"],
                {
                    "platform": item["platform"],
                    "name": platform_names.get(item["platform"], item["platform"]),
                    "cost": 0,
                    "value": 0,
                    "pnl": 0,
                    "pnl_pct": 0,
                },
            )
            platform["cost"] += cost
            platform["value"] += value
            platform["pnl"] += pnl

            if item["asset_type"] == "cash":
                cash = cash_by_platform.setdefault(
                    item["platform"],
                    {
                        "platform": item["platform"],
                        "name": platform_names.get(item["platform"], item["platform"]),
                        "amount": 0,
                        "currency": "KRW",
                    },
                )
                cash["amount"] += value
                continue

            valuation_status = _valuation_status(item, value)
            enriched = {
                **item,
                "display_name": item.get("alias") or item["name"],
                "cost": cost,
                "value": value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "valuation_status": valuation_status,
                "strategy_eligible": valuation_status == "priced",
            }
            by_symbol.append(enriched)
            if enriched["strategy_eligible"]:
                tradable_symbols.append(enriched)
            else:
                small_symbols.append(enriched)

        for item in by_platform.values():
            item["pnl_pct"] = (item["pnl"] / item["cost"] * 100) if item["cost"] else 0

        total_pnl = total_value - total_cost
        return {
            "total": {
                "cost": total_cost,
                "value": total_value,
                "pnl": total_pnl,
                "pnl_pct": (total_pnl / total_cost * 100) if total_cost else 0,
            },
            "by_platform": list(by_platform.values()),
            "by_symbol": by_symbol,
            "tradable_symbols": tradable_symbols,
            "small_symbols": small_symbols,
            "cash": {
                "total": sum(item["amount"] for item in cash_by_platform.values()),
                "by_platform": list(cash_by_platform.values()),
            },
            "exchange_rate": self.repo.latest_exchange_rate(),
            "dust_value_threshold": DUST_VALUE_THRESHOLD,
        }

    def sync_upbit_holdings(self) -> dict[str, Any]:
        return self._run_sync("upbit", self._sync_upbit_holdings)

    def _sync_upbit_holdings(self) -> dict[str, Any]:
        client = UpbitClient()
        accounts = client.accounts()
        markets = client.markets()
        market_names = {item["market"]: item.get("korean_name") or item["market"] for item in markets}
        available_markets = set(market_names)

        rows: list[dict[str, Any]] = []
        ticker_markets: list[str] = []
        crypto_accounts: list[dict[str, Any]] = []

        for account in accounts:
            currency = account.get("currency", "")
            balance = _to_float(account.get("balance"))
            locked = _to_float(account.get("locked"))
            quantity = balance + locked
            if quantity <= 0:
                continue

            if currency == "KRW":
                rows.append(
                    {
                        "symbol": "KRW",
                        "name": "원화",
                        "asset_type": "cash",
                        "quantity": quantity,
                        "avg_price": 1,
                        "current_price": 1,
                        "currency": "KRW",
                    }
                )
                continue

            unit_currency = account.get("unit_currency") or "KRW"
            market = f"{unit_currency}-{currency}"
            crypto_accounts.append(account)
            if market in available_markets:
                ticker_markets.append(market)

        tickers = {item["market"]: item for item in client.tickers(sorted(set(ticker_markets)))}
        for account in crypto_accounts:
            currency = account["currency"]
            unit_currency = account.get("unit_currency") or "KRW"
            market = f"{unit_currency}-{currency}"
            ticker = tickers.get(market, {})
            quantity = _to_float(account.get("balance")) + _to_float(account.get("locked"))
            avg_price = _to_float(account.get("avg_buy_price"))
            current_price = _to_float(ticker.get("trade_price")) or avg_price
            rows.append(
                {
                    "symbol": market,
                    "name": market_names.get(market, currency),
                    "asset_type": "crypto",
                    "quantity": quantity,
                    "avg_price": avg_price,
                    "current_price": current_price,
                    "currency": unit_currency,
                }
            )

        count = self.repo.replace_platform_holdings("upbit", rows)
        return {
            "platform": "upbit",
            "synced_count": count,
            "holdings": rows,
        }

    def sync_kis_holdings(self, platform: str | None = None) -> dict[str, Any]:
        results = []
        for account in kis_accounts():
            if platform and account.platform != platform:
                continue
            results.append(
                self._run_sync(
                    account.platform,
                    lambda account=account: self._sync_kis_account(account),
                )
            )
        return _group_sync_results(results)

    def _sync_kis_account(self, account) -> dict[str, Any]:
        client = KISClient(account)
        data = client.domestic_balance()
        rows = []
        cash_amount = _kis_orderable_cash(data)
        if cash_amount > 0:
            rows.append(
                {
                    "symbol": "KRW",
                    "name": "주문 가능 현금",
                    "asset_type": "cash",
                    "quantity": cash_amount,
                    "avg_price": 1,
                    "current_price": 1,
                    "currency": "KRW",
                }
            )
        for item in data.get("output1", []):
            quantity = _to_float(item.get("hldg_qty"))
            if quantity <= 0:
                continue
            avg_price = _to_float(item.get("pchs_avg_pric"))
            current_price = _to_float(item.get("prpr"))
            if current_price <= 0 and quantity:
                current_price = _to_float(item.get("evlu_amt")) / quantity
            rows.append(
                {
                    "symbol": item.get("pdno") or "UNKNOWN",
                    "name": item.get("prdt_name") or item.get("prdt_abrv_name") or item.get("pdno") or "UNKNOWN",
                    "asset_type": "stock",
                    "quantity": quantity,
                    "avg_price": avg_price,
                    "current_price": current_price or avg_price,
                    "currency": "KRW",
                }
            )
        count = self.repo.replace_platform_holdings(account.platform, rows)
        return {"platform": account.platform, "synced_count": count, "holdings": rows}

    def sync_toss_holdings(self) -> dict[str, Any]:
        return self._run_sync("toss", self._sync_toss_holdings)

    def _sync_toss_holdings(self) -> dict[str, Any]:
        client = TossInvestClient()
        data = client.holdings()
        exchange_rate = usd_krw_rate(self.repo, client)
        fx_rate = float(exchange_rate["rate"])
        rows = []
        krw_power = _to_float((client.buying_power("KRW").get("result") or {}).get("cashBuyingPower"))
        usd_power = _to_float((client.buying_power("USD").get("result") or {}).get("cashBuyingPower"))
        cash_amount = krw_power + usd_power * fx_rate
        if cash_amount > 0:
            rows.append(
                {
                    "symbol": "CASH",
                    "name": "주문 가능 현금",
                    "asset_type": "cash",
                    "quantity": cash_amount,
                    "avg_price": 1,
                    "current_price": 1,
                    "currency": "KRW",
                }
            )
        for item in (data.get("result") or {}).get("items", []):
            quantity = _to_float(item.get("quantity"))
            if quantity <= 0:
                continue
            currency = item.get("currency") or "USD"
            multiplier = fx_rate if currency == "USD" else 1
            rows.append(
                {
                    "symbol": item.get("symbol") or "UNKNOWN",
                    "name": item.get("name") or item.get("symbol") or "UNKNOWN",
                    "asset_type": "stock",
                    "quantity": quantity,
                    "avg_price": _to_float(item.get("averagePurchasePrice")) * multiplier,
                    "current_price": _to_float(item.get("lastPrice")) * multiplier,
                    "currency": "KRW",
                }
            )
        count = self.repo.replace_platform_holdings("toss", rows)
        return {
            "platform": "toss",
            "synced_count": count,
            "fx_usd_krw": fx_rate,
            "exchange_rate": exchange_rate,
            "holdings": rows,
        }

    def sync_all_holdings(self) -> dict[str, Any]:
        results = [
            {"source": "upbit", **self.sync_upbit_holdings()},
            {"source": "kis", **self.sync_kis_holdings()},
            {"source": "toss", **self.sync_toss_holdings()},
        ]
        statuses = [item["status"] for item in results]
        if all(status == "success" for status in statuses):
            status = "success"
        elif all(status == "failed" for status in statuses):
            status = "failed"
        else:
            status = "partial"
        return {"ok": status == "success", "status": status, "results": results}

    def run_due_dca_strategies(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(KST)
        runs = []
        for strategy in self.repo.strategies():
            schedule_key = scheduled_slot(strategy, now)
            if schedule_key:
                result = self._run_dca_strategy(strategy, trigger="scheduled", schedule_key=schedule_key)
                if result:
                    runs.append(result)
        return {"checked_at": now.astimezone(KST).isoformat(), "runs": runs}

    def run_dca_strategy_now(self, strategy_id: int) -> dict[str, Any]:
        strategy = self.repo.strategy(strategy_id)
        if not strategy:
            raise ValueError("전략을 찾을 수 없습니다.")
        if strategy["strategy_type"] != "dca":
            raise ValueError("DCA 전략만 DRY_RUN 테스트를 실행할 수 있습니다.")
        return self._run_dca_strategy(strategy, trigger="manual", schedule_key=None)

    def _run_dca_strategy(
        self,
        strategy: dict[str, Any],
        *,
        trigger: str,
        schedule_key: str | None,
    ) -> dict[str, Any] | None:
        run = self.repo.start_strategy_run(strategy["id"], trigger, schedule_key)
        if not run:
            return None

        try:
            items = strategy.get("params", {}).get("items") or []
            if not items:
                raise ValueError("DCA 주문 항목이 없습니다.")
            cost_assumptions = normalize_cost_assumptions(
                strategy.get("params", {}).get("cost_assumptions")
            )
            for item in items:
                compiled = compile_dca_buy_request(strategy["platform"], item)
                cost_estimate = estimate_dca_buy_cost(
                    item,
                    cost_assumptions,
                    reference_price=self._dca_reference_price(strategy["platform"], item),
                )
                request = {
                    "platform": strategy["platform"],
                    "symbol": item["symbol"],
                    "side": "buy",
                    "order_type": "market",
                    "quantity": item.get("quantity"),
                    "amount": item.get("amount"),
                    "currency": item.get("currency", "KRW"),
                    "dry_run": True,
                    "compiled_request": compiled,
                    "cost_assumptions": cost_assumptions,
                    **cost_estimate,
                }
                self.repo.create_order(
                    request,
                    status="dry_run",
                    reason="DRY_RUN: 실제 주문을 전송하지 않았습니다.",
                    strategy_run_id=run["id"],
                )
            return self.repo.finish_strategy_run(run["id"], status="success", order_count=len(items))
        except Exception as exc:
            return self.repo.finish_strategy_run(run["id"], status="failed", error=str(exc))

    def _dca_reference_price(self, platform: str, item: dict[str, Any]) -> float | None:
        if item.get("order_type") != "quantity":
            return None
        quote = self.repo.holding_quote(platform, item["symbol"])
        if not quote or quote["currency"] != item.get("currency", "KRW"):
            return None
        price = float(quote["current_price"])
        return price if price > 0 else None

    def _run_sync(self, platform: str, sync: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        sync_id = self.repo.start_sync(platform)
        try:
            result = sync()
        except Exception as exc:
            run = self.repo.finish_sync(sync_id, status="failed", error=str(exc))
            return {
                "platform": platform,
                "ok": False,
                "status": "failed",
                "error": str(exc),
                "started_at": run["started_at"],
                "completed_at": run["completed_at"],
            }

        count = int(result.get("synced_count", 0))
        run = self.repo.finish_sync(sync_id, status="success", synced_count=count)
        return {
            **result,
            "ok": True,
            "status": "success",
            "started_at": run["started_at"],
            "completed_at": run["completed_at"],
        }


def _group_sync_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    succeeded = sum(item["ok"] for item in results)
    if not results or succeeded == 0:
        status = "failed"
    elif succeeded == len(results):
        status = "success"
    else:
        status = "partial"
    return {"ok": status == "success", "status": status, "results": results}


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _kis_orderable_cash(data: dict[str, Any]) -> float:
    output2 = data.get("output2") or []
    if not output2:
        return 0.0
    summary = output2[0]
    return _to_float(summary.get("nxdy_excc_amt")) or _to_float(summary.get("dnca_tot_amt"))


def _valuation_status(item: dict[str, Any], value: float) -> str:
    if item["asset_type"] == "cash":
        return "cash" if value >= DUST_VALUE_THRESHOLD else "dust"
    if item["current_price"] <= 0:
        return "unpriced"
    if value < DUST_VALUE_THRESHOLD:
        return "dust"
    return "priced"
