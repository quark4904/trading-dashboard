from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from app.config import execution_history_days, platform_configs
from app.fee_policies import FeePolicyStore
from app.integrations.fx import usd_krw_rate
from app.integrations.kis import KISClient, kis_accounts
from app.integrations.tossinvest import TossInvestClient
from app.integrations.upbit import UpbitClient
from app.order_costs import (
    cost_overrides_from_params,
    dca_order_notional,
    estimate_dca_buy_cost,
)
from app.repository import Repository
from app.scheduler import KST, scheduled_slot
from app.strategy_capabilities import compile_dca_buy_request


DUST_VALUE_THRESHOLD = 100


class TradingService:
    def __init__(
        self,
        repo: Repository,
        *,
        fee_policy_store: FeePolicyStore | None = None,
        upbit_fee_provider: Callable[[str], dict[str, Any] | None] | None = None,
    ):
        self.repo = repo
        self.fee_policy_store = fee_policy_store or FeePolicyStore()
        self.upbit_fee_provider = upbit_fee_provider or self._fetch_upbit_fee

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

        start_date, end_date = _execution_history_range()
        closed_orders = _upbit_closed_orders(client, start_date, end_date)
        executions = [
            _normalize_upbit_execution(item, market_names, self.fee_policy_store)
            for item in _deduplicate_orders(closed_orders, "uuid")
            if _to_float(item.get("executed_volume")) > 0
        ]
        count = self.repo.replace_platform_holdings("upbit", rows)
        execution_count = self.repo.upsert_executions("upbit", executions)
        return {
            "platform": "upbit",
            "synced_count": count,
            "execution_count": execution_count,
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
        start_date, end_date = _execution_history_range()
        raw_executions = client.domestic_executions(
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
        )
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
        asset_types = {row["symbol"]: row["asset_type"] for row in rows}
        executions = [
            self._normalize_kis_execution(account.platform, item, asset_types)
            for item in _deduplicate_orders(raw_executions, "odno", prefix_fields=("ord_dt", "ord_gno_brno"))
            if _to_float(item.get("tot_ccld_qty")) > 0
        ]
        count = self.repo.replace_platform_holdings(account.platform, rows)
        execution_count = self.repo.upsert_executions(account.platform, executions)
        return {
            "platform": account.platform,
            "synced_count": count,
            "execution_count": execution_count,
            "holdings": rows,
        }

    def sync_toss_holdings(self) -> dict[str, Any]:
        return self._run_sync("toss", self._sync_toss_holdings)

    def _sync_toss_holdings(self) -> dict[str, Any]:
        client = TossInvestClient()
        data = client.holdings()
        start_date, end_date = _execution_history_range()
        raw_executions = client.closed_orders(
            from_date=start_date.isoformat(),
            to_date=end_date.isoformat(),
        )
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
        executions = [
            self._normalize_toss_execution(item)
            for item in _deduplicate_orders(raw_executions, "orderId")
            if _to_float((item.get("execution") or {}).get("filledQuantity")) > 0
        ]
        count = self.repo.replace_platform_holdings("toss", rows)
        execution_count = self.repo.upsert_executions("toss", executions)
        return {
            "platform": "toss",
            "synced_count": count,
            "execution_count": execution_count,
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
            cost_overrides = cost_overrides_from_params(strategy.get("params"))
            for item in items:
                compiled = compile_dca_buy_request(strategy["platform"], item)
                quote = self._dca_reference_quote(strategy["platform"], item)
                reference_price = float(quote["current_price"]) if quote else None
                notional = dca_order_notional(item, reference_price)
                live_fee_result = (
                    self.upbit_fee_provider(item["symbol"])
                    if strategy["platform"] == "upbit"
                    else None
                )
                live_fee = (
                    live_fee_result
                    if live_fee_result and live_fee_result.get("fee_pct") is not None
                    else None
                )
                cost_profile = self.fee_policy_store.resolve_cost_profile(
                    strategy["platform"],
                    item,
                    notional=notional,
                    asset_type=quote.get("asset_type") if quote else None,
                    cost_overrides=cost_overrides,
                    live_fee=live_fee,
                )
                if live_fee_result and live_fee_result.get("error"):
                    cost_profile["live_fee_lookup"] = {
                        "status": "fallback",
                        "error": str(live_fee_result["error"]),
                    }
                cost_estimate = estimate_dca_buy_cost(
                    item,
                    cost_profile,
                    reference_price=reference_price,
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
                    "cost_overrides": cost_overrides,
                    "cost_profile": cost_profile,
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

    def _dca_reference_quote(
        self,
        platform: str,
        item: dict[str, Any],
    ) -> dict[str, Any] | None:
        if item.get("order_type") != "quantity":
            return None
        quote = self.repo.holding_quote(platform, item["symbol"])
        if not quote or quote["currency"] != item.get("currency", "KRW"):
            return None
        price = float(quote["current_price"])
        return quote if price > 0 else None

    def _normalize_toss_execution(self, item: dict[str, Any]) -> dict[str, Any]:
        execution = item.get("execution") or {}
        amount = _to_float(execution.get("filledAmount"))
        quantity = _to_float(execution.get("filledQuantity"))
        average_price = _to_float(execution.get("averageFilledPrice"))
        if average_price <= 0 and quantity:
            average_price = amount / quantity
        currency = str(item.get("currency") or "KRW")
        market = "overseas" if currency == "USD" else "domestic"
        profile = self.fee_policy_store.resolve_cost_profile(
            "toss",
            {"market": market},
            notional=amount,
        )
        actual_fee = _optional_float(execution.get("commission"))
        actual_tax = _optional_float(execution.get("tax"))
        estimated_fee = None if actual_fee is not None else amount * profile["fee_pct"] / 100
        estimated_tax = None if actual_tax is not None else amount * profile["tax_pct"] / 100
        if actual_fee is not None:
            profile["fee_pct"] = actual_fee / amount * 100 if amount else 0
            profile["fee_source"] = {
                "kind": "actual_api",
                "label": "토스증권 체결 이력 수수료",
                "field": "execution.commission",
            }
        if actual_tax is not None:
            profile["tax_source"] = {
                "kind": "actual_api",
                "label": "토스증권 체결 이력 세금",
                "field": "execution.tax",
            }
        return {
            "external_order_id": str(item["orderId"]),
            "ordered_at": str(item.get("orderedAt") or execution.get("filledAt")),
            "executed_at": execution.get("filledAt"),
            "symbol": str(item.get("symbol") or "UNKNOWN"),
            "name": str(item.get("name") or item.get("symbol") or "UNKNOWN"),
            "side": str(item.get("side") or "").lower(),
            "order_type": str(item.get("orderType") or "unknown").lower(),
            "status": str(item.get("status") or "filled").lower(),
            "quantity": quantity,
            "average_price": average_price,
            "amount": amount,
            "currency": currency,
            "actual_fee": actual_fee,
            "estimated_fee": estimated_fee,
            "actual_tax": actual_tax,
            "estimated_tax": estimated_tax,
            "cost_profile": profile,
            "raw": item,
        }

    def _normalize_kis_execution(
        self,
        platform: str,
        item: dict[str, Any],
        asset_types: dict[str, str],
    ) -> dict[str, Any]:
        quantity = _to_float(item.get("tot_ccld_qty"))
        amount = _to_float(item.get("tot_ccld_amt"))
        average_price = _to_float(item.get("avg_prvs"))
        if average_price <= 0 and quantity:
            average_price = amount / quantity
        symbol = str(item.get("pdno") or "UNKNOWN")
        profile = self.fee_policy_store.resolve_cost_profile(
            platform,
            {"market": "domestic"},
            notional=amount,
            asset_type=asset_types.get(symbol),
        )
        side = "buy" if str(item.get("sll_buy_dvsn_cd")) == "02" else "sell"
        order_id = ":".join(
            [
                str(item.get("ord_dt") or ""),
                str(item.get("ord_gno_brno") or ""),
                str(item.get("odno") or ""),
            ]
        )
        remaining = _to_float(item.get("rmn_qty"))
        return {
            "external_order_id": order_id,
            "ordered_at": _kis_ordered_at(item),
            "executed_at": None,
            "symbol": symbol,
            "name": str(item.get("prdt_name") or symbol),
            "side": side,
            "order_type": str(item.get("ord_dvsn_name") or "unknown"),
            "status": "partial_filled" if remaining > 0 else "filled",
            "quantity": quantity,
            "average_price": average_price,
            "amount": amount,
            "currency": "KRW",
            "actual_fee": None,
            "estimated_fee": amount * profile["fee_pct"] / 100,
            "actual_tax": None,
            "estimated_tax": amount * profile["tax_pct"] / 100 if side == "buy" else None,
            "cost_profile": profile,
            "raw": item,
        }

    @staticmethod
    def _fetch_upbit_fee(market: str) -> dict[str, Any]:
        try:
            data = UpbitClient().order_chance(market)
            return {
                "fee_pct": float(data["bid_fee"]) * 100,
                "label": "업비트 주문 가능 정보 API",
                "url": "https://docs.upbit.com/kr/kr/reference/available-order-information",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as exc:
            return {"error": str(exc)}

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
        execution_count = int(result.get("execution_count", 0))
        run = self.repo.finish_sync(
            sync_id,
            status="success",
            synced_count=count,
            execution_count=execution_count,
        )
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


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _execution_history_range() -> tuple[date, date]:
    end_date = datetime.now(KST).date()
    return end_date - timedelta(days=execution_history_days() - 1), end_date


def _upbit_closed_orders(
    client: UpbitClient,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    start = datetime.combine(start_date, time.min, tzinfo=KST)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=KST)
    orders: list[dict[str, Any]] = []
    while start < end:
        window_end = min(start + timedelta(days=7), end)
        orders.extend(
            client.closed_orders(
                start_time=start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                end_time=window_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
        )
        start = window_end
    return orders


def _deduplicate_orders(
    rows: list[dict[str, Any]],
    id_field: str,
    *,
    prefix_fields: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    deduplicated: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = ":".join(str(row.get(field) or "") for field in (*prefix_fields, id_field))
        if key.strip(":"):
            deduplicated[key] = row
    return list(deduplicated.values())


def _normalize_upbit_execution(
    item: dict[str, Any],
    market_names: dict[str, str],
    fee_policy_store: FeePolicyStore,
) -> dict[str, Any]:
    amount = _to_float(item.get("executed_funds"))
    quantity = _to_float(item.get("executed_volume"))
    average_price = amount / quantity if quantity else 0
    actual_fee = _optional_float(item.get("paid_fee"))
    market = str(item.get("market") or "UNKNOWN")
    quote_currency = market.split("-", 1)[0] if "-" in market else "KRW"
    profile = fee_policy_store.resolve_cost_profile(
        "upbit",
        {"market": "crypto"},
        notional=amount,
    )
    estimated_fee = amount * profile["fee_pct"] / 100 if actual_fee is None else None
    if actual_fee is not None:
        profile["fee_pct"] = actual_fee / amount * 100 if amount else 0
        profile["fee_source"] = {
            "kind": "actual_api",
            "label": "업비트 종료 주문 사용 수수료",
            "field": "paid_fee",
        }
    return {
        "external_order_id": str(item["uuid"]),
        "ordered_at": str(item.get("created_at") or datetime.now(timezone.utc).isoformat()),
        "executed_at": None,
        "symbol": market,
        "name": market_names.get(market, market),
        "side": "buy" if item.get("side") == "bid" else "sell",
        "order_type": str(item.get("ord_type") or "unknown"),
        "status": str(item.get("state") or "done"),
        "quantity": quantity,
        "average_price": average_price,
        "amount": amount,
        "currency": quote_currency,
        "actual_fee": actual_fee,
        "estimated_fee": estimated_fee,
        "actual_tax": None,
        "estimated_tax": 0,
        "cost_profile": profile,
        "raw": item,
    }


def _kis_ordered_at(item: dict[str, Any]) -> str:
    raw_date = str(item.get("ord_dt") or "")
    raw_time = str(item.get("ord_tmd") or "").zfill(6)
    try:
        return datetime.strptime(f"{raw_date}{raw_time}", "%Y%m%d%H%M%S").replace(tzinfo=KST).isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()


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
