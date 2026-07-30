from __future__ import annotations

import math
from typing import Any


COST_RATE_FIELDS = ("fee_pct", "tax_pct", "slippage_pct")
DEFAULT_COST_ASSUMPTIONS = {field: 0.0 for field in COST_RATE_FIELDS}


def normalize_cost_assumptions(value: Any) -> dict[str, float]:
    if value is not None and not isinstance(value, dict):
        raise ValueError("거래 비용률 설정은 객체 형식이어야 합니다.")
    raw = value or {}
    result: dict[str, float] = {}
    for field in COST_RATE_FIELDS:
        try:
            rate = float(raw.get(field, DEFAULT_COST_ASSUMPTIONS[field]))
        except (TypeError, ValueError) as exc:
            raise ValueError("거래 비용률은 숫자여야 합니다.") from exc
        if not math.isfinite(rate) or not 0 <= rate <= 100:
            raise ValueError("거래 비용률은 0% 이상 100% 이하여야 합니다.")
        result[field] = rate
    return result


def estimate_dca_buy_cost(
    item: dict[str, Any],
    cost_assumptions: Any,
    *,
    reference_price: float | None = None,
) -> dict[str, float | None]:
    assumptions = normalize_cost_assumptions(cost_assumptions)
    notional = _order_notional(item, reference_price)
    if notional is None:
        return {
            "reference_price": None,
            "estimated_notional": None,
            "estimated_fee": None,
            "estimated_tax": None,
            "estimated_slippage": None,
            "estimated_total": None,
        }

    fee = _estimated_cost(notional, assumptions["fee_pct"])
    tax = _estimated_cost(notional, assumptions["tax_pct"])
    slippage = _estimated_cost(notional, assumptions["slippage_pct"])
    return {
        "reference_price": reference_price if item.get("order_type") == "quantity" else None,
        "estimated_notional": notional,
        "estimated_fee": fee,
        "estimated_tax": tax,
        "estimated_slippage": slippage,
        "estimated_total": round(notional + fee + tax + slippage, 8),
    }


def _order_notional(item: dict[str, Any], reference_price: float | None) -> float | None:
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
