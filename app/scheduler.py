from __future__ import annotations

import threading
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.services import TradingService


KST = ZoneInfo("Asia/Seoul")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def scheduled_slot(strategy: dict, now: datetime) -> str | None:
    """Return the KST minute key when a DCA strategy is due, otherwise None."""
    if not strategy.get("enabled") or strategy.get("strategy_type") != "dca":
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

    return now_kst.replace(second=0, microsecond=0).isoformat()


class StrategyScheduler:
    """Small in-process scheduler for DRY_RUN DCA execution.

    The database schedule key prevents duplicate executions when the scheduler
    checks the same minute more than once or an operator triggers it manually.
    """

    def __init__(self, service: "TradingService", interval_seconds: int = 15):
        self.service = service
        self.interval_seconds = interval_seconds
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
                print(f"Strategy scheduler failed: {exc}")
            self._stop.wait(self.interval_seconds)
