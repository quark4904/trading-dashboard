from __future__ import annotations

import math
from typing import Any


COST_RATE_FIELDS = ("fee_pct", "tax_pct", "slippage_pct")


def normalize_cost_overrides(value: Any) -> dict[str, float | None]:
    if value is not None and not isinstance(value, dict):
        raise ValueError("거래 비용률 설정은 객체 형식이어야 합니다.")
    raw = value or {}
    result: dict[str, float | None] = {}
    for field in COST_RATE_FIELDS:
        default = 0.0 if field == "slippage_pct" else None
        if field not in raw or raw[field] in (None, ""):
            result[field] = default
            continue
        try:
            rate = float(raw[field])
        except (TypeError, ValueError) as exc:
            raise ValueError("거래 비용률은 숫자여야 합니다.") from exc
        if not math.isfinite(rate) or not 0 <= rate <= 100:
            raise ValueError("거래 비용률은 0% 이상 100% 이하여야 합니다.")
        result[field] = rate
    return result


def cost_overrides_from_params(params: Any) -> dict[str, float | None]:
    values = params if isinstance(params, dict) else {}
    if "cost_overrides" in values:
        return normalize_cost_overrides(values.get("cost_overrides"))

    legacy = values.get("cost_assumptions")
    normalized = normalize_cost_overrides(legacy)
    legacy_rates = [rate or 0.0 for rate in normalized.values()]
    if legacy is None or not any(legacy_rates):
        return normalize_cost_overrides(None)
    return normalized


def estimate_dca_buy_cost(
    item: dict[str, Any],
    cost_profile: dict[str, Any],
    *,
    reference_price: float | None = None,
) -> dict[str, float | None]:
    notional = dca_order_notional(item, reference_price)
    if notional is None:
        return {
            "reference_price": None,
            "estimated_notional": None,
            "estimated_fee": None,
            "estimated_tax": None,
            "estimated_slippage": None,
            "estimated_total": None,
        }

    fee = _estimated_cost(notional, float(cost_profile["fee_pct"]))
    tax = _estimated_cost(notional, float(cost_profile["tax_pct"]))
    slippage = _estimated_cost(notional, float(cost_profile["slippage_pct"]))
    return {
        "reference_price": reference_price if item.get("order_type") == "quantity" else None,
        "estimated_notional": notional,
        "estimated_fee": fee,
        "estimated_tax": tax,
        "estimated_slippage": slippage,
        "estimated_total": round(notional + fee + tax + slippage, 8),
    }


def dca_order_notional(
    item: dict[str, Any],
    reference_price: float | None,
) -> float | None:
    if item.get("order_type") == "amount":
        amount = float(item["amount"])
        return round(amount, 8) if math.isfinite(amount) and amount >= 0 else None
    if item.get("order_type") != "quantity" or reference_price is None:
        return None
    quantity = float(item["quantity"])
    price = float(reference_price)
    if not all(math.isfinite(value) and value > 0 for value in (quantity, price)):
        return None
    return round(quantity * price, 8)


def _estimated_cost(notional: float, percentage: float) -> float:
    return round(notional * percentage / 100, 8)
