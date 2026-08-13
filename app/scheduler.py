from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.services import TradingService

from app.maintenance import automated_backup
from app.observability import configure_logging
from app.strategy_capabilities import DCA_STRATEGY_TYPE


KST = ZoneInfo("Asia/Seoul")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def scheduled_slot(strategy: dict, now: datetime) -> str | None:
    """Return the strategy-scoped KST minute key when a DCA strategy is due."""
    if not strategy.get("enabled") or strategy.get("strategy_type") != DCA_STRATEGY_TYPE:
        return None

    now_kst = now.astimezone(KST)
    params = strategy.get("params") or {}
    try:
        hour, minute = (int(value) for value in str(params.get("execution_time", "23:30")).split(":", 1))
    except ValueError:
        return None
    if (now_kst.hour, now_kst.minute) != (hour, minute):
        return None

    interval = params.get("interval", "daily")
    execution_day = params.get("execution_day")
    if interval == "weekly":
        weekday = execution_day if execution_day in WEEKDAYS else "monday"
        if now_kst.weekday() != WEEKDAYS.index(weekday):
            return None
    elif interval == "monthly":
        try:
            day = int(execution_day or 1)
        except (TypeError, ValueError):
            day = 1
        if now_kst.day != day:
            return None
    elif interval != "daily":
        return None

    slot = now_kst.replace(second=0, microsecond=0).isoformat()
    strategy_id = strategy.get("id")
    return f"{strategy_id}:{slot}" if strategy_id is not None else slot


class StrategyScheduler:
    """Small in-process scheduler for DRY_RUN DCA execution.

    The database schedule key prevents duplicate executions when the scheduler
    checks the same minute more than once or an operator triggers it manually.
    """

    def __init__(self, service: "TradingService", interval_seconds: int = 15):
        self.service = service
        self.interval_seconds = interval_seconds
        self.logger = configure_logging()
        self._last_backup_key: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="strategy-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 1)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.service.run_due_dca_strategies()
            except Exception as exc:  # Keep the dashboard server alive if a scheduler cycle fails.
                self.logger.exception("Strategy scheduler failed")
                self.service.repo.record_alert(
                    severity="error",
                    category="scheduler_failure",
                    message=f"스케줄러 실행 실패: {exc}",
                    details={"component": "strategy-scheduler"},
                    dedupe_key="scheduler-failure",
                )
            self._maybe_backup()
            self._stop.wait(self.interval_seconds)

    def _maybe_backup(self) -> None:
        if not os.getenv("TRADING_DASHBOARD_BACKUP_DIR", "").strip():
            return
        now = datetime.now(KST)
        backup_time = os.getenv("TRADING_DASHBOARD_BACKUP_TIME", "03:00")
        try:
            hour, minute = (int(value) for value in backup_time.split(":", 1))
        except (TypeError, ValueError):
            self.logger.error("잘못된 TRADING_DASHBOARD_BACKUP_TIME: %s", backup_time)
            return
        backup_key = now.date().isoformat()
        if (now.hour, now.minute) != (hour, minute) or self._last_backup_key == backup_key:
            return
        try:
            result = automated_backup(self.service.repo.db_path, now=now)
            if result:
                self._last_backup_key = backup_key
                self.logger.info("자동 백업 완료: %s", result["destination"])
        except Exception as exc:
            self.logger.exception("자동 백업 실패")
            self.service.repo.record_alert(
                severity="error",
                category="backup_failure",
                message=f"자동 백업 실패: {exc}",
                details={"backup_time": backup_time},
                dedupe_key="backup-failure",
            )
