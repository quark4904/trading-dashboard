from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import DB_PATH, env_flag


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS holdings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    avg_price REAL NOT NULL,
                    current_price REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'KRW',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_run_id INTEGER,
                    created_at TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    quantity REAL,
                    amount REAL,
                    currency TEXT NOT NULL DEFAULT 'KRW',
                    limit_price REAL,
                    dry_run INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    request_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS strategies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    strategy_type TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    platform TEXT,
                    symbol TEXT,
                    budget REAL NOT NULL DEFAULT 0,
                    take_profit_pct REAL,
                    stop_loss_pct REAL,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    synced_count INTEGER,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS strategy_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id INTEGER NOT NULL,
                    trigger TEXT NOT NULL,
                    schedule_key TEXT UNIQUE,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    order_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS asset_aliases (
                    platform TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (platform, symbol)
                );

                CREATE TABLE IF NOT EXISTS exchange_rates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    rate REAL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    error TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            exchange_rate_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(exchange_rates)").fetchall()
            }
            if "details_json" not in exchange_rate_columns:
                conn.execute("ALTER TABLE exchange_rates ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")
            order_columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
            if "strategy_run_id" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN strategy_run_id INTEGER")
            if "currency" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN currency TEXT NOT NULL DEFAULT 'KRW'")
            count = conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
            if count == 0 and env_flag("TRADING_DASHBOARD_SEED_DEMO"):
                self._seed(conn)

    def _seed(self, conn: sqlite3.Connection) -> None:
        now = utc_now()
        rows = [
            ("toss", "005930", "삼성전자", "stock", 8, 76000, 81200, "KRW", now),
            ("kis_pension", "069500", "KODEX 200", "etf", 12, 36500, 38100, "KRW", now),
            ("kis_isa", "379800", "KODEX 미국S&P500TR", "etf", 20, 15800, 17150, "KRW", now),
            ("upbit", "KRW-BTC", "비트코인", "crypto", 0.018, 89000000, 92500000, "KRW", now),
            ("upbit", "KRW-ETH", "이더리움", "crypto", 0.35, 4700000, 4520000, "KRW", now),
        ]
        conn.executemany(
            """
            INSERT INTO holdings
            (platform, symbol, name, asset_type, quantity, avg_price, current_price, currency, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.execute(
            """
            INSERT INTO strategies
            (name, strategy_type, enabled, platform, symbol, budget, take_profit_pct, stop_loss_pct, params_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "월간 리밸런싱 예시",
                "rebalance",
                0,
                None,
                None,
                1000000,
                8,
                -5,
                json.dumps({"interval": "monthly", "target_cash_pct": 10}, ensure_ascii=False),
                now,
                now,
            ),
        )

    def holdings(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT holdings.*, asset_aliases.alias
                FROM holdings
                LEFT JOIN asset_aliases
                  ON asset_aliases.platform = holdings.platform
                 AND asset_aliases.symbol = holdings.symbol
                ORDER BY holdings.platform, holdings.symbol
                """
            )
            return [dict(row) for row in rows]

    def replace_platform_holdings(self, platform: str, rows: list[dict[str, Any]]) -> int:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("DELETE FROM holdings WHERE platform = ?", (platform,))
            conn.executemany(
                """
                INSERT INTO holdings
                (platform, symbol, name, asset_type, quantity, avg_price, current_price, currency, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        platform,
                        row["symbol"],
                        row["name"],
                        row["asset_type"],
                        row["quantity"],
                        row["avg_price"],
                        row["current_price"],
                        row.get("currency", "KRW"),
                        now,
                    )
                    for row in rows
                ],
            )
            return len(rows)

    def start_sync(self, platform: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO sync_runs (platform, started_at, status)
                VALUES (?, ?, 'running')
                """,
                (platform, utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_sync(
        self,
        sync_id: int,
        *,
        status: str,
        synced_count: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sync_runs
                SET completed_at = ?, status = ?, synced_count = ?, error = ?
                WHERE id = ?
                """,
                (utc_now(), status, synced_count, error, sync_id),
            )
            row = conn.execute("SELECT * FROM sync_runs WHERE id = ?", (sync_id,)).fetchone()
            if not row:
                raise RuntimeError(f"sync run {sync_id} not found")
            return dict(row)

    def latest_sync_runs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT runs.*
                FROM sync_runs AS runs
                JOIN (
                    SELECT platform, MAX(id) AS id
                    FROM sync_runs
                    GROUP BY platform
                ) AS latest ON latest.id = runs.id
                ORDER BY runs.platform
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def recent_sync_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM sync_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def asset_aliases(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM asset_aliases ORDER BY platform, symbol"
            ).fetchall()
            return [dict(row) for row in rows]

    def set_asset_alias(self, platform: str, symbol: str, alias: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO asset_aliases (platform, symbol, alias, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(platform, symbol) DO UPDATE SET
                    alias = excluded.alias,
                    updated_at = excluded.updated_at
                """,
                (platform, symbol, alias, now),
            )
            row = conn.execute(
                "SELECT * FROM asset_aliases WHERE platform = ? AND symbol = ?",
                (platform, symbol),
            ).fetchone()
            return dict(row)

    def delete_asset_alias(self, platform: str, symbol: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM asset_aliases WHERE platform = ? AND symbol = ?",
                (platform, symbol),
            )
            return cursor.rowcount > 0

    def record_exchange_rate(
        self,
        *,
        pair: str,
        rate: float | None,
        source: str,
        status: str,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO exchange_rates (pair, rate, source, status, fetched_at, error, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (pair, rate, source, status, utc_now(), error, json.dumps(details or {}, ensure_ascii=False)),
            )
            row = conn.execute(
                "SELECT * FROM exchange_rates WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return self._exchange_rate_row(row)

    @staticmethod
    def _exchange_rate_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json") or "{}")
        return result

    def latest_exchange_rate(self, pair: str = "USD/KRW") -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM exchange_rates
                WHERE pair = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (pair,),
            ).fetchone()
            return self._exchange_rate_row(row) if row else None

    def latest_successful_exchange_rate(self, pair: str = "USD/KRW") -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM exchange_rates
                WHERE pair = ? AND status = 'success' AND rate IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (pair,),
            ).fetchone()
            return self._exchange_rate_row(row) if row else None

    def orders(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 100")]

    def create_order(
        self,
        request: dict[str, Any],
        status: str,
        reason: str,
        *,
        strategy_run_id: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO orders
                (strategy_run_id, created_at, platform, symbol, side, order_type, quantity, amount, currency, limit_price, dry_run, status, reason, request_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_run_id,
                    now,
                    request.get("platform"),
                    request.get("symbol"),
                    request.get("side"),
                    request.get("order_type", "market"),
                    request.get("quantity"),
                    request.get("amount"),
                    request.get("currency", "KRW"),
                    request.get("limit_price"),
                    1 if request.get("dry_run", True) else 0,
                    status,
                    reason,
                    json.dumps(request, ensure_ascii=False),
                ),
            )
            order_id = cursor.lastrowid
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return dict(row)

    def strategy(self, strategy_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
            return self._strategy_row(row) if row else None

    def start_strategy_run(self, strategy_id: int, trigger: str, schedule_key: str | None = None) -> dict[str, Any] | None:
        with self.connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO strategy_runs (strategy_id, trigger, schedule_key, started_at, status)
                    VALUES (?, ?, ?, ?, 'running')
                    """,
                    (strategy_id, trigger, schedule_key, utc_now()),
                )
            except sqlite3.IntegrityError:
                return None
            row = conn.execute("SELECT * FROM strategy_runs WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def finish_strategy_run(
        self,
        run_id: int,
        *,
        status: str,
        order_count: int = 0,
        error: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE strategy_runs
                SET completed_at = ?, status = ?, order_count = ?, error = ?
                WHERE id = ?
                """,
                (utc_now(), status, order_count, error, run_id),
            )
            row = conn.execute("SELECT * FROM strategy_runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                raise RuntimeError(f"strategy run {run_id} not found")
            return dict(row)

    def strategy_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self.connect() as conn:
            run_rows = conn.execute(
                """
                SELECT strategy_runs.*, COALESCE(strategies.name, '삭제된 전략') AS strategy_name
                FROM strategy_runs
                LEFT JOIN strategies ON strategies.id = strategy_runs.strategy_id
                ORDER BY strategy_runs.id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
            result = [dict(row) for row in run_rows]
            if not result:
                return result
            run_ids = [item["id"] for item in result]
            placeholders = ",".join("?" for _ in run_ids)
            order_rows = conn.execute(
                f"SELECT * FROM orders WHERE strategy_run_id IN ({placeholders}) ORDER BY id",
                run_ids,
            ).fetchall()
            orders_by_run: dict[int, list[dict[str, Any]]] = {}
            for row in order_rows:
                order = dict(row)
                orders_by_run.setdefault(order["strategy_run_id"], []).append(order)
            for run in result:
                run["orders"] = orders_by_run.get(run["id"], [])
            return result

    def strategies(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM strategies ORDER BY id DESC").fetchall()
            return [self._strategy_row(row) for row in rows]

    @staticmethod
    def _strategy_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["params"] = json.loads(item.pop("params_json") or "{}")
        return item

    def create_strategy(self, request: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        params = request.get("params") or {}
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO strategies
                (name, strategy_type, enabled, platform, symbol, budget, take_profit_pct, stop_loss_pct, params_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request["name"],
                    request.get("strategy_type", "custom"),
                    1 if request.get("enabled", False) else 0,
                    request.get("platform") or None,
                    request.get("symbol") or None,
                    float(request.get("budget") or 0),
                    request.get("take_profit_pct"),
                    request.get("stop_loss_pct"),
                    json.dumps(params, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return {"id": cursor.lastrowid, **request, "created_at": now, "updated_at": now}

    def update_strategy(self, strategy_id: int, request: dict[str, Any]) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as conn:
            existing = conn.execute("SELECT enabled, created_at FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
            if not existing:
                return None
            conn.execute(
                """
                UPDATE strategies
                SET name = ?, strategy_type = ?, platform = ?, symbol = ?, budget = ?,
                    take_profit_pct = ?, stop_loss_pct = ?, params_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    request["name"],
                    request.get("strategy_type", "custom"),
                    request.get("platform") or None,
                    request.get("symbol") or None,
                    float(request.get("budget") or 0),
                    request.get("take_profit_pct"),
                    request.get("stop_loss_pct"),
                    json.dumps(request.get("params") or {}, ensure_ascii=False),
                    now,
                    strategy_id,
                ),
            )
            row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
            return self._strategy_row(row)

    def set_strategy_enabled(self, strategy_id: int, enabled: bool) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE strategies SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, utc_now(), strategy_id),
            )
            row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
            if not row:
                return None
            return self._strategy_row(row)

    def delete_strategy(self, strategy_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))
            return cursor.rowcount > 0
