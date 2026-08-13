from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time
from typing import Any

from app.fee_policies import FeePolicyStore
from app.order_costs import (
    cost_overrides_from_params,
    dca_order_notional,
    estimate_dca_buy_cost,
)
from app.risk import KST, trading_session
from app.strategy_capabilities import dca_market_capability


MAX_BACKTEST_BARS = 20_000
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def run_dca_backtest(
    strategy: dict[str, Any],
    bars: list[dict[str, Any]],
    initial_cash: float,
    *,
    fee_policy_store: FeePolicyStore | None = None,
) -> dict[str, Any]:
    """Simulate a DCA strategy against timestamped historical prices.

    Each bar contains either a ``prices`` mapping or one ``symbol``/``price``
    pair. A scheduled order is filled on the first bar at or after the
    configured execution time on the due KST date. No broker or exchange API
    is called from this module.
    """

    if strategy.get("strategy_type") != "dca":
        raise ValueError("DCA 전략만 백테스트할 수 있습니다.")
    platform = str(strategy.get("platform") or "").strip()
    if not platform:
        raise ValueError("백테스트 전략의 플랫폼이 필요합니다.")

    params = strategy.get("params") if isinstance(strategy.get("params"), dict) else {}
    items = params.get("items") or []
    if not isinstance(items, list) or not items:
        raise ValueError("DCA 주문 항목이 없습니다.")
    normalized_items = _normalize_items(platform, items)
    currencies = {item["currency"] for item in normalized_items}
    if len(currencies) != 1:
        raise ValueError("백테스트에서는 하나의 통화만 사용할 수 있습니다.")
    currency = next(iter(currencies))

    starting_cash = _positive_number(initial_cash, "초기 현금")
    normalized_bars = _normalize_bars(bars)
    cost_store = fee_policy_store or FeePolicyStore()
    cost_overrides = cost_overrides_from_params(params)
    max_orders_per_day, daily_budget = _risk_limits(strategy)
    if currency != "KRW" and daily_budget > 0:
        raise ValueError("원화 기준 일일 예산은 KRW 백테스트에서만 사용할 수 있습니다.")

    cash = starting_cash
    positions: dict[str, dict[str, float]] = {}
    last_prices: dict[str, float] = {}
    daily_order_counts: defaultdict[date, int] = defaultdict(int)
    daily_spend: defaultdict[date, float] = defaultdict(float)
    seen_schedule_keys: set[str] = set()
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    totals = {"fee": 0.0, "tax": 0.0, "slippage": 0.0}

    for bar in normalized_bars:
        timestamp = bar["timestamp"]
        prices = bar["prices"]
        last_prices.update(prices)
        schedule_key = _backtest_schedule_key(strategy, timestamp)
        if schedule_key and schedule_key not in seen_schedule_keys:
            seen_schedule_keys.add(schedule_key)
            for item in normalized_items:
                symbol = item["symbol"]
                price = prices.get(symbol)
                if price is None:
                    trades.append(
                        _rejected_trade(
                            timestamp,
                            item,
                            "해당 시점의 가격 데이터가 없습니다.",
                        )
                    )
                    continue

                market = item["market"]
                session = trading_session(platform, market, timestamp)
                if not session["ok"]:
                    trades.append(_rejected_trade(timestamp, item, session.get("reason") or "거래 시간이 아닙니다."))
                    continue

                reference_notional = dca_order_notional(item, price)
                if reference_notional is None or reference_notional <= 0:
                    trades.append(_rejected_trade(timestamp, item, "주문 예상 금액을 산출할 수 없습니다."))
                    continue

                profile = cost_store.resolve_cost_profile(
                    platform,
                    item,
                    notional=reference_notional,
                    asset_type=item.get("asset_type") or ("crypto" if platform == "upbit" else "stock"),
                    cost_overrides=cost_overrides,
                )
                estimate = estimate_dca_buy_cost(item, profile, reference_price=price)
                total = estimate["estimated_total"]
                if total is None:
                    trades.append(_rejected_trade(timestamp, item, "주문 예상 비용을 산출할 수 없습니다."))
                    continue
                total = float(total)
                current_day = timestamp.astimezone(KST).date()
                if daily_order_counts[current_day] >= max_orders_per_day:
                    trades.append(
                        _rejected_trade(
                            timestamp,
                            item,
                            f"일일 최대 주문 횟수 {max_orders_per_day}건을 초과했습니다.",
                        )
                    )
                    continue
                if daily_budget > 0 and daily_spend[current_day] + total > daily_budget:
                    trades.append(
                        _rejected_trade(
                            timestamp,
                            item,
                            f"일일 예산 {daily_budget:g}을 초과했습니다.",
                        )
                    )
                    continue
                if cash < total:
                    trades.append(_rejected_trade(timestamp, item, "가상 현금이 부족합니다."))
                    continue

                slippage_pct = float(profile["slippage_pct"])
                execution_price = price * (1 + slippage_pct / 100)
                if item["order_type"] == "amount":
                    quantity = float(item["amount"]) / execution_price
                else:
                    quantity = float(item["quantity"])
                if not math.isfinite(quantity) or quantity <= 0:
                    trades.append(_rejected_trade(timestamp, item, "체결 수량을 산출할 수 없습니다."))
                    continue

                actual_notional = quantity * execution_price
                fee = float(estimate["estimated_fee"] or 0)
                tax = float(estimate["estimated_tax"] or 0)
                slippage = float(estimate["estimated_slippage"] or 0)
                cash -= total
                position = positions.setdefault(symbol, {"quantity": 0.0, "cost_basis": 0.0})
                position["quantity"] += quantity
                position["cost_basis"] += total
                daily_order_counts[current_day] += 1
                daily_spend[current_day] += total
                totals["fee"] += fee
                totals["tax"] += tax
                totals["slippage"] += slippage
                trades.append(
                    {
                        "timestamp": timestamp.isoformat(),
                        "symbol": symbol,
                        "side": "buy",
                        "status": "filled",
                        "currency": currency,
                        "quantity": _rounded(quantity),
                        "reference_price": _rounded(price),
                        "execution_price": _rounded(execution_price),
                        "reference_notional": _rounded(reference_notional),
                        "notional": _rounded(actual_notional),
                        "fee": _rounded(fee),
                        "tax": _rounded(tax),
                        "slippage": _rounded(slippage),
                        "total": _rounded(total),
                        "cost_profile": profile,
                    }
                )

        equity_curve.append(_equity_snapshot(timestamp, cash, positions, last_prices, starting_cash))

    final_holdings = _final_holdings(positions, last_prices, currency)
    final_value = cash + sum(float(item["value"]) for item in final_holdings)
    filled_count = sum(1 for trade in trades if trade["status"] == "filled")
    rejected_count = len(trades) - filled_count
    return {
        "strategy_id": strategy.get("id"),
        "strategy_name": strategy.get("name"),
        "platform": platform,
        "currency": currency,
        "initial_cash": _rounded(starting_cash),
        "final_cash": _rounded(cash),
        "final_value": _rounded(final_value),
        "pnl": _rounded(final_value - starting_cash),
        "return_pct": _rounded((final_value - starting_cash) / starting_cash * 100),
        "fees": _rounded(totals["fee"]),
        "taxes": _rounded(totals["tax"]),
        "slippage": _rounded(totals["slippage"]),
        "trade_count": filled_count,
        "rejected_count": rejected_count,
        "scheduled_count": len(seen_schedule_keys),
        "start_at": normalized_bars[0]["timestamp"].isoformat(),
        "end_at": normalized_bars[-1]["timestamp"].isoformat(),
        "holdings": final_holdings,
        "trades": trades,
        "equity_curve": equity_curve,
        "assumptions": {
            "fill_rule": "해당 날짜의 실행 시각 이후 첫 번째 가격 바",
            "prices_timezone": "입력 타임존 유지, 일정 판정은 Asia/Seoul",
            "fees": "전략 비용 설정과 로컬 fee-policies.json",
            "live_api_called": False,
        },
    }


def _normalize_items(platform: str, items: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("DCA 자산 설정을 확인하세요.")
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            raise ValueError("백테스트 DCA 종목을 확인하세요.")
        market, capability = dca_market_capability(platform, raw.get("market"))
        order_type = str(raw.get("order_type") or capability["order_mode"])
        if order_type != capability["order_mode"]:
            raise ValueError(f"{platform}/{market}에서는 {capability['order_mode']} 주문만 지원합니다.")
        raw_value = raw.get("amount") if order_type == "amount" else raw.get("quantity")
        value = _positive_number(raw_value, f"{symbol} 주문 값")
        if capability["integer_only"] and order_type == "quantity" and not value.is_integer():
            raise ValueError(f"{symbol} 주문 수량은 정수여야 합니다.")
        currency = str(raw.get("currency") or capability["currency"])
        if currency != capability["currency"]:
            raise ValueError(f"{platform}/{market} 주문 통화는 {capability['currency']}여야 합니다.")
        normalized_item = {
            "symbol": symbol,
            "market": market,
            "order_type": order_type,
            "currency": currency,
        }
        if order_type == "amount":
            normalized_item["amount"] = value
        else:
            normalized_item["quantity"] = int(value) if value.is_integer() else value
        if raw.get("asset_type"):
            normalized_item["asset_type"] = str(raw["asset_type"])
        normalized.append(normalized_item)
        seen.add(symbol)
    return normalized


def _normalize_bars(bars: Any) -> list[dict[str, Any]]:
    if not isinstance(bars, list) or not bars:
        raise ValueError("가격 바를 하나 이상 입력하세요.")
    if len(bars) > MAX_BACKTEST_BARS:
        raise ValueError(f"가격 바는 {MAX_BACKTEST_BARS}개 이하로 입력하세요.")

    normalized: list[dict[str, Any]] = []
    seen_timestamps: set[datetime] = set()
    for raw in bars:
        if not isinstance(raw, dict):
            raise ValueError("가격 바 형식이 올바르지 않습니다.")
        timestamp = _parse_timestamp(raw.get("timestamp"))
        if timestamp in seen_timestamps:
            raise ValueError("가격 바의 timestamp는 중복될 수 없습니다.")
        prices = raw.get("prices")
        if not isinstance(prices, dict):
            symbol = str(raw.get("symbol") or "").strip().upper()
            if not symbol or raw.get("price") is None:
                raise ValueError("가격 바에는 prices 또는 symbol/price가 필요합니다.")
            prices = {symbol: raw.get("price")}
        normalized_prices: dict[str, float] = {}
        for symbol, value in prices.items():
            normalized_symbol = str(symbol).strip().upper()
            if not normalized_symbol:
                raise ValueError("가격 바의 종목 코드가 비어 있습니다.")
            normalized_prices[normalized_symbol] = _positive_number(value, f"{normalized_symbol} 가격")
        if not normalized_prices:
            raise ValueError("가격 바의 prices가 비어 있습니다.")
        normalized.append({"timestamp": timestamp, "prices": normalized_prices})
        seen_timestamps.add(timestamp)
    normalized.sort(key=lambda item: item["timestamp"])
    return normalized


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("가격 바 timestamp가 필요합니다.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("가격 바 timestamp는 ISO 8601 형식이어야 합니다.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)


def _positive_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}은 숫자여야 합니다.") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label}은 0보다 커야 합니다.")
    return number


def _risk_limits(strategy: dict[str, Any]) -> tuple[int, float]:
    params = strategy.get("params") if isinstance(strategy.get("params"), dict) else {}
    limits = params.get("risk_limits") if isinstance(params.get("risk_limits"), dict) else {}
    raw_max_orders = limits.get("max_orders_per_day", 20)
    try:
        max_orders_value = float(raw_max_orders)
    except (TypeError, ValueError) as exc:
        raise ValueError("일일 최대 주문 횟수가 올바르지 않습니다.") from exc
    if not math.isfinite(max_orders_value) or not max_orders_value.is_integer() or max_orders_value < 1:
        raise ValueError("일일 최대 주문 횟수는 1 이상이어야 합니다.")
    max_orders = int(max_orders_value)
    raw_budget = limits.get("daily_budget_krw", strategy.get("budget", 0))
    try:
        budget = float(raw_budget or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("일일 예산이 올바르지 않습니다.") from exc
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("일일 예산은 0 이상인 숫자여야 합니다.")
    return max_orders, budget


def _backtest_schedule_key(strategy: dict[str, Any], timestamp: datetime) -> str | None:
    params = strategy.get("params") if isinstance(strategy.get("params"), dict) else {}
    execution_time = str(params.get("execution_time") or "23:30")
    try:
        hour, minute = (int(value) for value in execution_time.split(":", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("실행 시간은 HH:MM 형식이어야 합니다.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("실행 시간은 HH:MM 형식이어야 합니다.")

    current = timestamp.astimezone(KST)
    interval = params.get("interval", "daily")
    if interval == "weekly":
        execution_day = str(params.get("execution_day") or "monday").lower()
        if execution_day not in WEEKDAYS or current.weekday() != WEEKDAYS.index(execution_day):
            return None
    elif interval == "monthly":
        try:
            execution_day = int(params.get("execution_day") or 1)
        except (TypeError, ValueError) as exc:
            raise ValueError("월간 실행일이 올바르지 않습니다.") from exc
        if current.day != execution_day:
            return None
    elif interval != "daily":
        raise ValueError("지원하지 않는 DCA 실행 주기입니다.")

    target = datetime.combine(current.date(), time(hour, minute), tzinfo=KST)
    if current < target:
        return None
    return target.isoformat()


def _rejected_trade(timestamp: datetime, item: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "timestamp": timestamp.isoformat(),
        "symbol": item["symbol"],
        "side": "buy",
        "status": "rejected",
        "currency": item["currency"],
        "reason": reason,
    }


def _equity_snapshot(
    timestamp: datetime,
    cash: float,
    positions: dict[str, dict[str, float]],
    prices: dict[str, float],
    starting_cash: float,
) -> dict[str, Any]:
    holdings_value = sum(
        position["quantity"] * prices.get(symbol, 0)
        for symbol, position in positions.items()
    )
    total_value = cash + holdings_value
    return {
        "timestamp": timestamp.isoformat(),
        "cash": _rounded(cash),
        "holdings_value": _rounded(holdings_value),
        "total_value": _rounded(total_value),
        "pnl": _rounded(total_value - starting_cash),
        "return_pct": _rounded((total_value - starting_cash) / starting_cash * 100),
    }


def _final_holdings(
    positions: dict[str, dict[str, float]],
    prices: dict[str, float],
    currency: str,
) -> list[dict[str, Any]]:
    result = []
    for symbol, position in sorted(positions.items()):
        quantity = position["quantity"]
        cost = position["cost_basis"]
        current_price = prices.get(symbol)
        if current_price is None:
            current_price = cost / quantity
        value = quantity * current_price
        result.append(
            {
                "symbol": symbol,
                "quantity": _rounded(quantity),
                "average_price": _rounded(cost / quantity),
                "current_price": _rounded(current_price),
                "cost": _rounded(cost),
                "value": _rounded(value),
                "pnl": _rounded(value - cost),
                "pnl_pct": _rounded((value - cost) / cost * 100 if cost else 0),
                "currency": currency,
            }
        )
    return result


def _rounded(value: float) -> float:
    return round(float(value), 8)
