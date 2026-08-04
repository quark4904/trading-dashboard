from __future__ import annotations

import math
import re

from app.config import platform_configs
from app.order_costs import cost_overrides_from_params
from app.risk import DEFAULT_MAX_ORDERS_PER_DAY
from app.strategy_capabilities import dca_market_capability


def validate_strategy(data: dict) -> dict:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("전략 이름은 필수입니다.")
    if len(name) > 100:
        raise ValueError("전략 이름은 100자 이하여야 합니다.")

    strategy_type = str(data.get("strategy_type") or "custom")
    if strategy_type not in {"dca", "rebalance", "momentum", "mean_reversion", "custom"}:
        raise ValueError("지원하지 않는 전략 유형입니다.")

    platform = str(data.get("platform") or "").strip()
    if platform and platform not in {item.code for item in platform_configs()}:
        raise ValueError("지원하지 않는 플랫폼입니다.")

    symbol = str(data.get("symbol") or "").strip()
    if len(symbol) > 50:
        raise ValueError("종목 코드는 50자 이하여야 합니다.")

    budget = validated_number(data.get("budget", 0), "예산")
    if budget < 0:
        raise ValueError("예산은 0 이상이어야 합니다.")

    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    if strategy_type == "dca":
        if not platform:
            raise ValueError("DCA 전략의 플랫폼을 선택하세요.")
        raw_items = params.get("items")
        if isinstance(raw_items, list):
            items = []
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    raise ValueError("DCA 자산 설정을 확인하세요.")
                item_symbol = str(raw_item.get("symbol") or "").strip().upper()
                raw_value = raw_item.get("value")
                if raw_value is None:
                    raw_value = raw_item.get("quantity") if raw_item.get("order_type") == "quantity" else raw_item.get("amount", raw_item.get("amount_usd"))
                item_value = validated_number(raw_value, f"{item_symbol or 'DCA 자산'} 주문 값")
                market_value = raw_item.get("market")
                market = str(market_value) if market_value else None
                items.append(normalize_dca_item(platform, item_symbol, item_value, market))
        else:
            legacy_value = params.get("quantity", 1) if platform.startswith("kis_") else params.get("amount_usd", 1)
            legacy_value = validated_number(legacy_value, "종목당 주문 값")
            items = [
                normalize_dca_item(platform, item.strip().upper(), legacy_value)
                for item in symbol.split(",")
                if item.strip()
            ]
        if not items:
            raise ValueError("DCA 종목 코드를 하나 이상 입력하세요.")
        symbols = [item["symbol"] for item in items]
        if len(items) > 20 or any(not item.replace(".", "").replace("-", "").isalnum() for item in symbols):
            raise ValueError("DCA 종목 코드를 확인하세요. 최대 20개까지 입력할 수 있습니다.")
        if any(dca_item_value(item) <= 0 for item in items):
            raise ValueError("자산별 주문 값은 0보다 커야 합니다.")
        if len(set(symbols)) != len(symbols):
            raise ValueError("DCA 전략에 같은 종목을 중복해서 입력할 수 없습니다.")
        interval = str(params.get("interval") or "daily")
        if interval not in {"daily", "weekly", "monthly"}:
            raise ValueError("지원하지 않는 DCA 실행 주기입니다.")
        execution_time = str(params.get("execution_time") or "23:30")
        match = re.fullmatch(r"(\d{2}):(\d{2})", execution_time)
        if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise ValueError("실행 시간은 HH:MM 형식이어야 합니다.")
        execution_day = params.get("execution_day")
        if interval == "weekly":
            execution_day = str(execution_day or "monday").lower()
            if execution_day not in {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}:
                raise ValueError("주간 DCA 실행 요일을 확인하세요.")
        elif interval == "monthly":
            try:
                execution_day = int(execution_day or 1)
            except (TypeError, ValueError) as exc:
                raise ValueError("월간 DCA 실행일을 확인하세요.") from exc
            if not 1 <= execution_day <= 28:
                raise ValueError("월간 DCA 실행일은 1일부터 28일까지 선택할 수 있습니다.")
        raw_risk_limits = params.get("risk_limits") if isinstance(params.get("risk_limits"), dict) else {}
        max_orders_per_day = validated_number(
            params.get("max_orders_per_day", raw_risk_limits.get("max_orders_per_day", DEFAULT_MAX_ORDERS_PER_DAY)),
            "일일 최대 주문 횟수",
        )
        if not max_orders_per_day.is_integer() or not 1 <= max_orders_per_day <= 100:
            raise ValueError("일일 최대 주문 횟수는 1부터 100까지의 정수여야 합니다.")
        symbol = ",".join(symbols)
        params = {
            "items": items,
            "interval": interval,
            "execution_time": execution_time,
            "cost_overrides": cost_overrides_from_params(params),
            "risk_limits": {
                "daily_budget_krw": budget,
                "max_orders_per_day": int(max_orders_per_day),
            },
        }
        if interval in {"weekly", "monthly"}:
            params["execution_day"] = execution_day

    result = {
        "name": name,
        "strategy_type": strategy_type,
        "enabled": False,
        "platform": platform,
        "symbol": symbol,
        "budget": budget,
        "params": params,
    }
    for key, label in [("take_profit_pct", "익절률"), ("stop_loss_pct", "손절률")]:
        value = data.get(key)
        if value is not None:
            result[key] = validated_number(value, label)
    return result


def validated_number(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}은 숫자여야 합니다.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}은 유한한 숫자여야 합니다.")
    return number


def normalize_dca_item(platform: str, symbol: str, value: float, market: str | None = None) -> dict:
    selected_market, capability = dca_market_capability(platform, market)
    if value < float(capability["value_min"]):
        raise ValueError(f"{capability['value_label']}은 {capability['value_min']} 이상이어야 합니다.")
    if capability["integer_only"] and not value.is_integer():
        raise ValueError(f"{capability['value_label']}은 정수여야 합니다.")
    item = {
        "symbol": symbol,
        "market": selected_market,
        "order_type": capability["order_mode"],
        "currency": capability["currency"],
    }
    if capability["order_mode"] == "quantity":
        item["quantity"] = int(value)
    else:
        item["amount"] = value
    return item


def dca_item_value(item: dict) -> float:
    return float(item["quantity"] if item["order_type"] == "quantity" else item["amount"])
