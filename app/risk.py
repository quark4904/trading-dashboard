from __future__ import annotations

import hashlib
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_MAX_ORDERS_PER_DAY = 20
CANCELLATION_POLICY = "reject_before_submission"


def make_idempotency_key(strategy_id: int, run_id: int, item_index: int, symbol: str) -> str:
    """Return a stable key short enough for broker APIs that cap identifiers at 36 chars."""
    digest = hashlib.sha256(
        f"dca:{strategy_id}:{run_id}:{item_index}:{symbol}".encode("utf-8")
    ).hexdigest()[:24]
    return f"dca-{strategy_id}-{digest}"[:36]


def trading_session(platform: str, market: str, now: datetime) -> dict[str, Any]:
    current = _as_kst(now)
    if platform == "upbit":
        return {
            "ok": True,
            "label": "24시간 거래",
            "timezone": "Asia/Seoul",
            "checked_at": current.isoformat(),
        }

    if market == "overseas":
        current = current.astimezone(NEW_YORK)
        start, end = time(9, 30), time(16, 0)
        label = "미국 정규장 09:30~16:00 ET"
        timezone_name = "America/New_York"
    else:
        start, end = time(9, 0), time(15, 30)
        label = "국내 정규장 09:00~15:30 KST"
        timezone_name = "Asia/Seoul"

    opened = current.weekday() < 5 and start <= current.time().replace(tzinfo=None) < end
    return {
        "ok": opened,
        "label": label,
        "timezone": timezone_name,
        "checked_at": current.isoformat(),
        "reason": None if opened else "거래 시간이 아닙니다.",
    }


def parse_upbit_order_chance(data: Any, symbol: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {
            "ok": False,
            "errors": ["업비트 주문 가능 정보 응답이 없습니다."],
        }
    if data.get("error"):
        return {"ok": False, "errors": [str(data["error"])]}

    market = data.get("market") or {}
    market_id = str(market.get("id") or "")
    state = str(market.get("state") or "").lower()
    order_types = {str(value).lower() for value in (market.get("order_types") or [])}
    order_sides = {str(value).lower() for value in (market.get("order_sides") or [])}
    min_total = _optional_float(market.get("min_total"))
    max_total = _optional_float(market.get("max_total"))
    bid_fee = _optional_float(data.get("bid_fee"))
    bid_account = data.get("bid_account") or {}
    balance = _optional_float(bid_account.get("balance"))

    errors: list[str] = []
    if market_id and market_id != symbol:
        errors.append(f"업비트 종목 응답이 요청 종목({symbol})과 다릅니다.")
    if state and state != "active":
        errors.append(f"업비트 종목 상태가 {state}입니다.")
    if "price" not in order_types:
        errors.append("업비트 금액 시장가 매수가 지원되지 않습니다.")
    if order_sides and "bid" not in order_sides:
        errors.append("업비트 매수 주문이 지원되지 않습니다.")
    if min_total is None:
        errors.append("업비트 최소 주문 금액을 확인할 수 없습니다.")

    return {
        "ok": not errors,
        "errors": errors,
        "market": market_id or symbol,
        "state": state or "unknown",
        "order_types": sorted(order_types),
        "order_sides": sorted(order_sides),
        "min_total": min_total,
        "max_total": max_total,
        "bid_fee_pct": bid_fee * 100 if bid_fee is not None else None,
        "bid_balance": balance,
    }


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)
