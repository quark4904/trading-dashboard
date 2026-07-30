from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from typing import Any
from urllib.error import HTTPError
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen

from app.config import load_env


class UpbitError(RuntimeError):
    pass


class UpbitClient:
    base_url = "https://api.upbit.com"

    def __init__(self) -> None:
        load_env()
        self.access_key = os.getenv("UPBIT_ACCESS_KEY", "")
        self.secret_key = os.getenv("UPBIT_SECRET_KEY", "")
        if not self.access_key or not self.secret_key:
            raise UpbitError("UPBIT_ACCESS_KEY 또는 UPBIT_SECRET_KEY가 없습니다.")

    def accounts(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/accounts")

    def markets(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/market/all", auth=False, params={"isDetails": "false"})

    def tickers(self, markets: list[str]) -> list[dict[str, Any]]:
        if not markets:
            return []
        return self._request("GET", "/v1/ticker", auth=False, params={"markets": ",".join(markets)})

    def order_chance(self, market: str) -> dict[str, Any]:
        return self._request("GET", "/v1/orders/chance", params={"market": market})

    def closed_orders(
        self,
        *,
        start_time: str,
        end_time: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/v1/orders/closed",
            params={
                "start_time": start_time,
                "end_time": end_time,
                "limit": str(limit),
                "order_by": "asc",
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        params: dict[str, Any] | None = None,
    ) -> Any:
        query = unquote(urlencode(params or {}, doseq=True))
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._jwt(query)}"

        try:
            with urlopen(Request(url, headers=headers, method=method), timeout=12) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise UpbitError(f"Upbit API {exc.code}: {body}") from exc
        except OSError as exc:
            raise UpbitError(f"Upbit API 연결 실패: {exc}") from exc

    def _jwt(self, query: str = "") -> str:
        payload: dict[str, Any] = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
        }
        if query:
            payload["query_hash"] = hashlib.sha512(query.encode("utf-8")).hexdigest()
            payload["query_hash_alg"] = "SHA512"

        header = {"alg": "HS512", "typ": "JWT"}
        signing_input = ".".join([_b64_json(header), _b64_json(payload)])
        signature = hmac.new(self.secret_key.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha512).digest()
        return f"{signing_input}.{_b64(signature)}"


def _b64_json(data: dict[str, Any]) -> str:
    return _b64(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
