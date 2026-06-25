from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen


def usd_krw_rate() -> float:
    configured = _to_float(os.getenv("USD_KRW_RATE"))
    if configured > 0:
        return configured

    try:
        req = Request("https://open.er-api.com/v6/latest/USD", headers={"Accept": "application/json"})
        with urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        rate = _to_float((data.get("rates") or {}).get("KRW"))
        if rate > 0:
            return rate
    except OSError:
        pass

    return 1400.0


def _to_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
