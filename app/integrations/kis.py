from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import load_env
from app.integrations.http import HTTPTransportError, request_json, retry_policy


class KISError(RuntimeError):
    pass


@dataclass(frozen=True)
class KISAccount:
    platform: str
    app_key: str
    app_secret: str
    account_no: str
    product_code: str
    label: str
    category: str


class KISClient:
    def __init__(self, account: KISAccount) -> None:
        load_env()
        self.account = account
        self.is_paper = os.getenv("KIS_IS_PAPER", "false").lower() == "true"
        self.base_url = "https://openapivts.koreainvestment.com:29443" if self.is_paper else "https://openapi.koreainvestment.com:9443"
        self._retry_policy = retry_policy("kis", timeout_seconds=12)
        if not account.app_key or not account.app_secret or not account.account_no or not account.product_code:
            raise KISError(f"{account.platform} 한국투자증권 설정값이 부족합니다.")

    def domestic_balance(self) -> dict[str, Any]:
        params = {
            "CANO": self.account.account_no,
            "ACNT_PRDT_CD": self.account.product_code,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        tr_id = "VTTC8434R" if self.is_paper else "TTTC8434R"
        return self._request("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", params=params, tr_id=tr_id)

    def domestic_executions(self, *, start_date: str, end_date: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        fk100 = ""
        nk100 = ""
        tr_cont = ""
        tr_id = "VTTC0081R" if self.is_paper else "TTTC0081R"
        for _ in range(10):
            params = {
                "CANO": self.account.account_no,
                "ACNT_PRDT_CD": self.account.product_code,
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "SLL_BUY_DVSN_CD": "00",
                "PDNO": "",
                "CCLD_DVSN": "01",
                "INQR_DVSN": "00",
                "INQR_DVSN_3": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": fk100,
                "CTX_AREA_NK100": nk100,
                "EXCG_ID_DVSN_CD": "ALL",
            }
            data = self._request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                params=params,
                tr_id=tr_id,
                tr_cont=tr_cont,
                include_response_headers=True,
            )
            if str(data.get("rt_cd") or "0") != "0":
                raise KISError(
                    f"{self.account.platform} 체결 이력 조회 실패: "
                    f"{data.get('msg1') or data.get('msg_cd') or data}"
                )
            rows.extend(data.get("output1") or [])
            headers = data.pop("_response_headers", {})
            next_fk100 = str(data.get("ctx_area_fk100") or "")
            next_nk100 = str(data.get("ctx_area_nk100") or "")
            if headers.get("tr_cont") not in {"M", "F"} or (next_fk100, next_nk100) == (fk100, nk100):
                break
            fk100, nk100, tr_cont = next_fk100, next_nk100, "N"
        return rows

    def _token(self) -> str:
        cache_key = self.account.app_key
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and cached["expires_at"] > time.time() + 60:
            return cached["access_token"]

        payload = json.dumps(
            {
                "grant_type": "client_credentials",
                "appkey": self.account.app_key,
                "appsecret": self.account.app_secret,
            }
        ).encode("utf-8")
        data = self._request_raw(
            "POST",
            "/oauth2/tokenP",
            body=payload,
            headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        )
        token = data.get("access_token")
        if not token:
            raise KISError(f"{self.account.platform} 토큰 발급 실패: {data}")
        expires_in = _to_int(data.get("expires_in")) or 86400
        _TOKEN_CACHE[cache_key] = {"access_token": token, "expires_at": time.time() + expires_in}
        return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        tr_id: str,
        tr_cont: str = "",
        include_response_headers: bool = False,
    ) -> dict[str, Any]:
        token = self._token()
        query = urlencode(params or {})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        return self._request_raw(
            method,
            path,
            url=url,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": self.account.app_key,
                "appsecret": self.account.app_secret,
                "tr_id": tr_id,
                "tr_cont": tr_cont,
            },
            include_response_headers=include_response_headers,
        )

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        url: str | None = None,
        include_response_headers: bool = False,
    ) -> dict[str, Any]:
        try:
            data, response_headers = request_json(
                Request(url or f"{self.base_url}{path}", data=body, headers=headers or {}, method=method),
                provider="kis",
                opener=urlopen,
                policy=getattr(self, "_retry_policy", retry_policy("kis", timeout_seconds=12)),
            )
            if include_response_headers:
                data["_response_headers"] = response_headers
            return data
        except HTTPTransportError as exc:
            if exc.status is not None:
                raise KISError(f"{self.account.platform} KIS API {exc.status}: {exc.body[:4000]}") from exc
            raise KISError(f"{self.account.platform} KIS API 연결 실패: {exc}") from exc


def kis_accounts() -> list[KISAccount]:
    load_env()
    return [
        KISAccount(
            platform="kis_pension",
            app_key=os.getenv("KIS_PENSION_APP_KEY", ""),
            app_secret=os.getenv("KIS_PENSION_APP_SECRET", ""),
            account_no=os.getenv("KIS_PENSION_ACCOUNT_NO", ""),
            product_code=os.getenv("KIS_PENSION_ACCOUNT_PRODUCT_CODE", ""),
            label=os.getenv("KIS_PENSION_LABEL", "한국투자증권 연금"),
            category=os.getenv("KIS_PENSION_CATEGORY", "stock"),
        ),
        KISAccount(
            platform="kis_isa",
            app_key=os.getenv("KIS_ISA_APP_KEY", ""),
            app_secret=os.getenv("KIS_ISA_APP_SECRET", ""),
            account_no=os.getenv("KIS_ISA_ACCOUNT_NO", ""),
            product_code=os.getenv("KIS_ISA_ACCOUNT_PRODUCT_CODE", ""),
            label=os.getenv("KIS_ISA_LABEL", "한국투자증권 ISA"),
            category=os.getenv("KIS_ISA_CATEGORY", "stock"),
        ),
    ]


_TOKEN_CACHE: dict[str, dict[str, Any]] = {}


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
