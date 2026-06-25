from __future__ import annotations

import os
from dataclasses import dataclass
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
            category=os.getenv("TOSSINVEST_ACCOUNT_CATEGORY", "stock"),
            required_env=("TOSSINVEST_CLIENT_ID", "TOSSINVEST_CLIENT_SECRET", "TOSSINVEST_ACCOUNT_SEQ"),
        ),
        PlatformConfig(
            code="kis_pension",
            name=os.getenv("KIS_PENSION_LABEL", "한국투자증권 연금"),
            category=os.getenv("KIS_PENSION_CATEGORY", "stock"),
            required_env=("KIS_PENSION_APP_KEY", "KIS_PENSION_APP_SECRET", "KIS_PENSION_ACCOUNT_NO"),
        ),
        PlatformConfig(
            code="kis_isa",
            name=os.getenv("KIS_ISA_LABEL", "한국투자증권 ISA"),
            category=os.getenv("KIS_ISA_CATEGORY", "stock"),
            required_env=("KIS_ISA_APP_KEY", "KIS_ISA_APP_SECRET", "KIS_ISA_ACCOUNT_NO"),
        ),
        PlatformConfig(
            code="upbit",
            name="업비트",
            category="crypto",
            required_env=("UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY"),
        ),
    ]


load_env()
