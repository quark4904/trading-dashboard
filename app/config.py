from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"
DB_PATH = Path(os.getenv("TRADING_DASHBOARD_DB_PATH", ROOT_DIR / "data" / "trading_dashboard.db"))


def load_env(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def execution_history_days() -> int:
    load_env()
    try:
        days = int(os.getenv("TRADING_DASHBOARD_EXECUTION_HISTORY_DAYS", "90"))
    except ValueError:
        days = 90
    return max(1, min(days, 90))


@dataclass(frozen=True)
class PlatformConfig:
    code: str
    name: str
    category: str
    required_env: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return all(bool(os.getenv(key)) for key in self.required_env)


def platform_configs() -> list[PlatformConfig]:
    return [
        PlatformConfig(
            code="toss",
            name=os.getenv("TOSSINVEST_ACCOUNT_LABEL", "토스증권"),
            category=os.getenv("TOSSINVEST_ACCOUNT_CATEGORY", "주식(해외/국내)"),
            required_env=("TOSSINVEST_CLIENT_ID", "TOSSINVEST_CLIENT_SECRET", "TOSSINVEST_ACCOUNT_SEQ"),
        ),
        PlatformConfig(
            code="kis_pension",
            name=os.getenv("KIS_PENSION_LABEL", "한국투자증권 연금"),
            category=os.getenv("KIS_PENSION_CATEGORY", "stock"),
            required_env=(
                "KIS_PENSION_APP_KEY",
                "KIS_PENSION_APP_SECRET",
                "KIS_PENSION_ACCOUNT_NO",
                "KIS_PENSION_ACCOUNT_PRODUCT_CODE",
            ),
        ),
        PlatformConfig(
            code="kis_isa",
            name=os.getenv("KIS_ISA_LABEL", "한국투자증권 ISA"),
            category=os.getenv("KIS_ISA_CATEGORY", "stock"),
            required_env=(
                "KIS_ISA_APP_KEY",
                "KIS_ISA_APP_SECRET",
                "KIS_ISA_ACCOUNT_NO",
                "KIS_ISA_ACCOUNT_PRODUCT_CODE",
            ),
        ),
        PlatformConfig(
            code="upbit",
            name="업비트",
            category="crypto",
            required_env=("UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY"),
        ),
    ]


def api_key_expirations(today: date | None = None) -> list[dict[str, str | int | None]]:
    today = today or date.today()
    configs = [
        ("upbit", "업비트", ("UPBIT_KEY_EXPIRES_ON", "UPBIT_API_KEY_EXPIRES_AT")),
        (
            "toss",
            os.getenv("TOSSINVEST_ACCOUNT_LABEL", "토스증권"),
            ("TOSSINVEST_KEY_EXPIRES_ON", "TOSSINVEST_API_KEY_EXPIRES_AT"),
        ),
        (
            "kis_pension",
            os.getenv("KIS_PENSION_LABEL", "한국투자증권 연금"),
            ("KIS_PENSION_KEY_EXPIRES_ON", "KIS_PENSION_API_KEY_EXPIRES_AT"),
        ),
        (
            "kis_isa",
            os.getenv("KIS_ISA_LABEL", "한국투자증권 ISA"),
            ("KIS_ISA_KEY_EXPIRES_ON", "KIS_ISA_API_KEY_EXPIRES_AT"),
        ),
    ]
    results = []
    for platform, name, env_names in configs:
        value = next((os.getenv(env_name, "").strip() for env_name in env_names if os.getenv(env_name, "").strip()), "")
        if not value:
            results.append(
                {
                    "platform": platform,
                    "name": name,
                    "expires_at": None,
                    "days_remaining": None,
                    "status": "unknown",
                }
            )
            continue
        try:
            expires_at = date.fromisoformat(value)
        except ValueError:
            results.append(
                {
                    "platform": platform,
                    "name": name,
                    "expires_at": value,
                    "days_remaining": None,
                    "status": "invalid",
                }
            )
            continue

        days_remaining = (expires_at - today).days
        status = "expired" if days_remaining < 0 else "warning" if days_remaining <= 30 else "valid"
        results.append(
            {
                "platform": platform,
                "name": name,
                "expires_at": expires_at.isoformat(),
                "days_remaining": days_remaining,
                "status": status,
            }
        )
    return results


load_env()
