from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from app.repository import Repository


class FxError(RuntimeError):
    pass


def usd_krw_rate(repo: "Repository") -> dict[str, Any]:
    configured = _to_float(os.getenv("USD_KRW_RATE"))
    if configured > 0:
        return repo.record_exchange_rate(
            pair="USD/KRW",
            rate=configured,
            source="configured",
            status="success",
        )

    try:
        req = Request("https://open.er-api.com/v6/latest/USD", headers={"Accept": "application/json"})
        with urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        rate = _to_float((data.get("rates") or {}).get("KRW"))
        if rate <= 0:
            raise FxError("환율 API 응답에 USD/KRW 값이 없습니다.")
        return repo.record_exchange_rate(
            pair="USD/KRW",
            rate=rate,
            source="open.er-api.com",
            status="success",
        )
    except (OSError, ValueError, FxError) as exc:
        cached = repo.latest_successful_exchange_rate()
        error = str(exc)
        if cached:
            return repo.record_exchange_rate(
                pair="USD/KRW",
                rate=cached["rate"],
                source="cached",
                status="stale",
                error=error,
            )
        repo.record_exchange_rate(
            pair="USD/KRW",
            rate=None,
            source="open.er-api.com",
            status="failed",
            error=error,
        )
        raise FxError(f"USD/KRW 환율 조회 실패: {error}") from exc


def _to_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
