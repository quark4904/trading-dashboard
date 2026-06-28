from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import load_env


class TossInvestError(RuntimeError):
    pass


class TossInvestClient:
    def __init__(self) -> None:
        load_env()
        self.base_url = os.getenv("TOSSINVEST_BASE_URL", "https://openapi.tossinvest.com").rstrip("/")
        self.client_id = os.getenv("TOSSINVEST_CLIENT_ID", "")
        self.client_secret = os.getenv("TOSSINVEST_CLIENT_SECRET", "")
        self.account_seq = os.getenv("TOSSINVEST_ACCOUNT_SEQ", "")
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

    def _token(self) -> str:
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
        return token

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
            with urlopen(Request(f"{self.base_url}{path}", data=body, headers=headers or {}, method=method), timeout=12) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise TossInvestError(f"토스증권 API {exc.code}: {body_text}") from exc
        except OSError as exc:
            raise TossInvestError(f"토스증권 API 연결 실패: {exc}") from exc
