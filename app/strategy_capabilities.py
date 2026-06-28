from __future__ import annotations

from copy import deepcopy
from typing import Any


_CAPABILITIES: dict[str, dict[str, Any]] = {
    "toss": {
        "default_market": "overseas",
        "markets": {
            "domestic": {
                "label": "국내주식",
                "symbol_placeholder": "예: 005930",
                "order_mode": "quantity",
                "value_label": "매수 수량 (주)",
                "value_step": 1,
                "value_min": 1,
                "integer_only": True,
                "currency": "KRW",
                "order_type": "MARKET",
            },
            "overseas": {
                "label": "미국주식",
                "symbol_placeholder": "예: SCHD",
                "order_mode": "amount",
                "value_label": "매수 금액 (USD)",
                "value_step": 0.01,
                "value_min": 0.01,
                "integer_only": False,
                "currency": "USD",
                "order_type": "MARKET",
                "regular_hours_only": True,
            },
        },
    },
    "kis_pension": {
        "default_market": "domestic",
        "markets": {
            "domestic": {
                "label": "국내주식",
                "symbol_placeholder": "예: 360750",
                "order_mode": "quantity",
                "value_label": "매수 수량 (주)",
                "value_step": 1,
                "value_min": 1,
                "integer_only": True,
                "currency": "KRW",
                "order_type": "MARKET",
                "exchange_code": "KRX",
            }
        },
    },
    "kis_isa": {
        "default_market": "domestic",
        "markets": {
            "domestic": {
                "label": "국내주식",
                "symbol_placeholder": "예: 458730",
                "order_mode": "quantity",
                "value_label": "매수 수량 (주)",
                "value_step": 1,
                "value_min": 1,
                "integer_only": True,
                "currency": "KRW",
                "order_type": "MARKET",
                "exchange_code": "KRX",
            }
        },
    },
    "upbit": {
        "default_market": "crypto",
        "markets": {
            "crypto": {
                "label": "가상자산",
                "symbol_placeholder": "예: KRW-BTC",
                "order_mode": "amount",
                "value_label": "매수 금액 (KRW)",
                "value_step": 1,
                "value_min": 5000,
                "integer_only": False,
                "currency": "KRW",
                "order_type": "MARKET",
                "preflight": "GET /v1/orders/chance?market={symbol}",
            }
        },
    },
}


def strategy_capabilities() -> dict[str, Any]:
    return {"platforms": deepcopy(_CAPABILITIES)}


def dca_market_capability(platform: str, market: str | None = None) -> tuple[str, dict[str, Any]]:
    platform_capability = _CAPABILITIES.get(platform)
    if not platform_capability:
        raise ValueError("DCA를 지원하지 않는 플랫폼입니다.")
    selected_market = market or platform_capability["default_market"]
    market_capability = platform_capability["markets"].get(selected_market)
    if not market_capability:
        raise ValueError("선택한 플랫폼에서 지원하지 않는 시장입니다.")
    return selected_market, market_capability


def compile_dca_buy_request(platform: str, item: dict[str, Any]) -> dict[str, Any]:
    market, capability = dca_market_capability(platform, item.get("market"))
    symbol = item["symbol"]
    if platform == "toss":
        request = {"symbol": symbol, "side": "BUY", "orderType": "MARKET"}
        if capability["order_mode"] == "quantity":
            request["quantity"] = str(item["quantity"])
        else:
            request["orderAmount"] = str(item["amount"])
        return {"method": "POST", "path": "/api/v1/orders", "market": market, "body": request}
    if platform in {"kis_pension", "kis_isa"}:
        return {
            "method": "POST",
            "path": "/uapi/domestic-stock/v1/trading/order-cash",
            "market": market,
            "body": {
                "PDNO": symbol,
                "ORD_DVSN": "01",
                "ORD_QTY": str(item["quantity"]),
                "ORD_UNPR": "0",
                "EXCG_ID_DVSN_CD": capability["exchange_code"],
            },
        }
    if platform == "upbit":
        return {
            "method": "POST",
            "path": "/v1/orders",
            "market": market,
            "body": {
                "market": symbol,
                "side": "bid",
                "ord_type": "price",
                "price": str(item["amount"]),
            },
        }
    raise ValueError("DCA 주문 요청을 생성할 수 없는 플랫폼입니다.")
