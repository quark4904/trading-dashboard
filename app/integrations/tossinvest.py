from __future__ import annotations

import base64
import os
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import load_env
from app.integrations.http import HTTPTransportError, request_json, retry_policy


class TossInvestError(RuntimeError):
    pass


class TossInvestClient:
    def __init__(self) -> None:
        load_env()
        self.base_url = os.getenv("TOSSINVEST_BASE_URL", "https://openapi.tossinvest.com").rstrip("/")
        self.client_id = os.getenv("TOSSINVEST_CLIENT_ID", "")
        self.client_secret = os.getenv("TOSSINVEST_CLIENT_SECRET", "")
        self.account_seq = os.getenv("TOSSINVEST_ACCOUNT_SEQ", "")
        self._access_token = ""
        self._token_expires_at = 0.0
        self._retry_policy = retry_policy("toss", timeout_seconds=12)
        if not self.client_id or not self.client_secret or not self.account_seq:
            raise TossInvestError("토스증권 설정값이 부족합니다.")

    def accounts(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/v1/accounts")
        return data.get("result", [])

    def holdings(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/holdings", headers={"x-tossinvest-account": self.account_seq})

    def buying_power(self, currency: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/buying-power?currency={currency}", headers={"x-tossinvest-account": self.account_seq})

    def exchange_rate(self, base_currency: str = "USD", quote_currency: str = "KRW") -> dict[str, Any]:
        query = urlencode({"baseCurrency": base_currency, "quoteCurrency": quote_currency})
        return self._request("GET", f"/api/v1/exchange-rate?{query}")

    def closed_orders(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        while True:
            params = {
                "status": "CLOSED",
                "from": from_date,
                "to": to_date,
                "limit": "100",
            }
            if cursor:
                params["cursor"] = cursor
            data = self._request(
                "GET",
                f"/api/v1/orders?{urlencode(params)}",
                headers={"x-tossinvest-account": self.account_seq},
            )
            result = data.get("result") or {}
            orders.extend(result.get("orders") or [])
            next_cursor = str(result.get("nextCursor") or "")
            if not result.get("hasNext") or not next_cursor or next_cursor in seen_cursors:
                return orders
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def _token(self) -> str:
        if self._access_token and self._token_expires_at > time.time() + 60:
            return self._access_token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("utf-8")).decode("ascii")
        payload = urlencode({"grant_type": "client_credentials"}).encode("utf-8")
        data = self._request_raw(
            "POST",
            "/oauth2/token",
            body=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}",
            },
            auth=False,
        )
        token = data.get("access_token")
        if not token:
            raise TossInvestError(f"토스증권 토큰 발급 실패: {data}")
        self._access_token = str(token)
        self._token_expires_at = time.time() + int(data.get("expires_in") or 86400)
        return self._access_token

    def _request(self, method: str, path: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        token = self._token()
        merged_headers = {"Accept": "application/json", "Authorization": f"Bearer {token}", **(headers or {})}
        return self._request_raw(method, path, headers=merged_headers)

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        try:
            data, _ = request_json(
                Request(f"{self.base_url}{path}", data=body, headers=headers or {}, method=method),
                provider="toss",
                opener=urlopen,
                policy=getattr(self, "_retry_policy", retry_policy("toss", timeout_seconds=12)),
            )
            return data
        except HTTPTransportError as exc:
            if exc.status is not None:
                raise TossInvestError(f"토스증권 API {exc.status}: {exc.body[:4000]}") from exc
            raise TossInvestError(f"토스증권 API 연결 실패: {exc}") from exc
