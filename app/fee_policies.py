from __future__ import annotations

import json
import math
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR
from app.order_costs import normalize_cost_overrides


DEFAULT_FEE_POLICY_PATH = ROOT_DIR / "config" / "fee-policies.json"


class FeePolicyError(RuntimeError):
    pass


class FeePolicyStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def resolve_cost_profile(
        self,
        platform: str,
        item: dict[str, Any],
        *,
        notional: float | None,
        asset_type: str | None = None,
        cost_overrides: Any = None,
        live_fee: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        policy = self._load()
        rule = self._market_rule(policy, platform, str(item.get("market") or ""))
        overrides = normalize_cost_overrides(cost_overrides)

        fee_pct, fee_rule = self._policy_fee(rule, item, notional, asset_type)
        fee_source = {
            "kind": "official_policy",
            **deepcopy(rule["source"]),
            "policy_version": policy["policy_version"],
            "rule": fee_rule,
        }
        tax_pct = _validated_rate(rule.get("buy_tax_pct", 0), "매수 세율")
        tax_source = {
            "kind": "official_policy",
            "label": "매수 주문 공식 기본 세율",
            "policy_version": policy["policy_version"],
        }

        if overrides["fee_pct"] is not None:
            fee_pct = overrides["fee_pct"]
            fee_source = {"kind": "user_override", "label": "사용자 수수료 설정"}
        if overrides["tax_pct"] is not None:
            tax_pct = overrides["tax_pct"]
            tax_source = {"kind": "user_override", "label": "사용자 세금 설정"}

        if live_fee is not None:
            fee_pct = _validated_rate(live_fee.get("fee_pct"), "실시간 수수료율")
            fee_source = {
                "kind": "live_api",
                "label": str(live_fee.get("label") or "실시간 수수료 조회"),
                "url": str(live_fee.get("url") or ""),
                "checked_at": str(live_fee.get("checked_at") or ""),
            }

        slippage_pct = overrides["slippage_pct"] or 0.0
        return {
            "fee_pct": fee_pct,
            "tax_pct": tax_pct,
            "slippage_pct": slippage_pct,
            "fee_source": fee_source,
            "tax_source": tax_source,
            "slippage_source": {
                "kind": "user_assumption",
                "label": "사용자 슬리피지 가정",
            },
        }

    def _load(self) -> dict[str, Any]:
        path = self.path or Path(
            os.getenv("TRADING_DASHBOARD_FEE_POLICY_PATH", DEFAULT_FEE_POLICY_PATH)
        )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FeePolicyError(f"수수료 정책 파일을 찾을 수 없습니다: {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise FeePolicyError(f"수수료 정책 파일을 읽을 수 없습니다: {path}") from exc
        if data.get("schema_version") != 1 or not isinstance(data.get("platforms"), dict):
            raise FeePolicyError("지원하지 않는 수수료 정책 파일 형식입니다.")
        if not str(data.get("policy_version") or "").strip():
            raise FeePolicyError("수수료 정책 버전이 없습니다.")
        return data

    @staticmethod
    def _market_rule(policy: dict[str, Any], platform: str, market: str) -> dict[str, Any]:
        platform_policy = policy["platforms"].get(platform)
        if not isinstance(platform_policy, dict):
            raise FeePolicyError(f"{platform} 수수료 정책이 없습니다.")
        markets = platform_policy.get("markets")
        rule = markets.get(market) if isinstance(markets, dict) else None
        if not isinstance(rule, dict) or not isinstance(rule.get("source"), dict):
            raise FeePolicyError(f"{platform}/{market} 수수료 정책이 없습니다.")
        return rule

    @staticmethod
    def _policy_fee(
        rule: dict[str, Any],
        item: dict[str, Any],
        notional: float | None,
        asset_type: str | None,
    ) -> tuple[float, str]:
        fee_pct = _validated_rate(rule.get("fee_pct"), "기본 수수료율")
        rule_label = "기본 공식 요율"

        venue_rates = rule.get("venue_fee_pct")
        if isinstance(venue_rates, dict):
            venue = str(item.get("venue") or rule.get("default_venue") or "")
            if venue in venue_rates:
                fee_pct = _validated_rate(venue_rates[venue], f"{venue} 수수료율")
                rule_label = f"{venue} 공식 요율"

        asset_rates = rule.get("asset_type_fee_pct")
        if isinstance(asset_rates, dict):
            selected_type = str(asset_type or rule.get("default_asset_type") or "")
            if selected_type in asset_rates:
                fee_pct = _validated_rate(
                    asset_rates[selected_type],
                    f"{selected_type} 수수료율",
                )
                rule_label = f"{selected_type} 공식 요율"

        waiver = rule.get("fee_waiver")
        if isinstance(waiver, dict) and notional is not None:
            threshold = float(waiver.get("notional_lte", -1))
            if math.isfinite(threshold) and notional <= threshold:
                fee_pct = _validated_rate(waiver.get("fee_pct", 0), "면제 수수료율")
                rule_label = str(waiver.get("label") or "공식 수수료 면제")

        return fee_pct, rule_label


def _validated_rate(value: Any, label: str) -> float:
    try:
        rate = float(value)
    except (TypeError, ValueError) as exc:
        raise FeePolicyError(f"{label}이 숫자가 아닙니다.") from exc
    if not math.isfinite(rate) or not 0 <= rate <= 100:
        raise FeePolicyError(f"{label}은 0% 이상 100% 이하여야 합니다.")
    return rate
