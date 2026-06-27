from __future__ import annotations

import math

from app.config import platform_configs


def validate_strategy(data: dict) -> dict:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("전략 이름은 필수입니다.")
    if len(name) > 100:
        raise ValueError("전략 이름은 100자 이하여야 합니다.")

    strategy_type = str(data.get("strategy_type") or "custom")
    if strategy_type not in {"rebalance", "momentum", "mean_reversion", "custom"}:
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

    result = {
        "name": name,
        "strategy_type": strategy_type,
        "enabled": data.get("enabled") is True,
        "platform": platform,
        "symbol": symbol,
        "budget": budget,
        "params": data.get("params") if isinstance(data.get("params"), dict) else {},
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
