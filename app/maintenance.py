from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.config import DB_PATH
from app.repository import SCHEMA_VERSION


def integrity_check(db_path: Path) -> dict[str, str | bool]:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"SQLite 데이터베이스가 없습니다: {path}")
    with sqlite3.connect(path) as conn:
        result = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    return {"ok": result.lower() == "ok", "result": result}


def migration_status(db_path: Path) -> dict[str, object]:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"SQLite 데이터베이스가 없습니다: {path}")
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        history = []
        if table:
            history = [
                dict(row)
                for row in conn.execute(
                    "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
    current = int(history[-1]["version"]) if history else 0
    return {"current_version": current, "latest_version": SCHEMA_VERSION, "history": history}


def backup_database(
    source: Path = DB_PATH,
    destination: Path | None = None,
    *,
    overwrite: bool = False,
) -> dict[str, object]:
    source = Path(source)
    if destination is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = source.with_name(f"{source.stem}-{timestamp}{source.suffix}")
    destination = Path(destination)
    _validate_copy_paths(source, destination)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"백업 파일이 이미 있습니다: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        _copy_database(source, temporary)
        check = integrity_check(temporary)
        if not check["ok"]:
            raise RuntimeError(f"백업 무결성 검사 실패: {check['result']}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "source": str(source),
        "destination": str(destination),
        "integrity": check,
    }


def restore_database(
    source: Path,
    destination: Path = DB_PATH,
    *,
    overwrite: bool = False,
) -> dict[str, object]:
    source = Path(source)
    destination = Path(destination)
    _validate_copy_paths(source, destination)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"복구 대상이 이미 있습니다. --force가 필요합니다: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.restore.tmp")
    try:
        _copy_database(source, temporary)
        check = integrity_check(temporary)
        if not check["ok"]:
            raise RuntimeError(f"복구 파일 무결성 검사 실패: {check['result']}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "source": str(source),
        "destination": str(destination),
        "integrity": check,
    }


def automated_backup(
    source: Path = DB_PATH,
    backup_dir: Path | None = None,
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, object] | None:
    configured_dir = os.getenv("TRADING_DASHBOARD_BACKUP_DIR", "").strip()
    if backup_dir is None:
        if not configured_dir:
            return None
        backup_dir = Path(configured_dir)
    backup_dir = Path(backup_dir)
    if retention_days is None:
        retention_days = _env_int("TRADING_DASHBOARD_BACKUP_RETENTION_DAYS", 7, minimum=1, maximum=365)
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"{Path(source).stem}-{stamp}{Path(source).suffix}"
    result = backup_database(source, destination)

    cutoff = now.timestamp() - timedelta(days=retention_days).total_seconds()
    removed: list[str] = []
    pattern = f"{Path(source).stem}-*.db"
    for candidate in backup_dir.glob(pattern):
        if candidate == destination or not candidate.is_file() or candidate.is_symlink():
            continue
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed.append(str(candidate))
        except OSError:
            continue
    return {**result, "removed": removed, "retention_days": retention_days}


def _copy_database(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as source_conn, sqlite3.connect(destination) as destination_conn:
        source_conn.backup(destination_conn)


def _validate_copy_paths(source: Path, destination: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"SQLite 데이터베이스가 없습니다: {source}")
    if source.resolve() == destination.resolve():
        raise ValueError("원본과 대상은 서로 다른 경로여야 합니다.")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def main() -> None:
    parser = argparse.ArgumentParser(description="Trading Dashboard SQLite 유지보수 도구")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="일관된 SQLite 백업 생성")
    backup_parser.add_argument("--source", type=Path, default=DB_PATH)
    backup_parser.add_argument("--output", type=Path, required=True)
    backup_parser.add_argument("--force", action="store_true")

    restore_parser = subparsers.add_parser("restore", help="SQLite 백업 복구")
    restore_parser.add_argument("--source", type=Path, required=True)
    restore_parser.add_argument("--target", type=Path, default=DB_PATH)
    restore_parser.add_argument("--force", action="store_true")

    integrity_parser = subparsers.add_parser("integrity", help="SQLite 무결성 검사")
    integrity_parser.add_argument("--database", type=Path, default=DB_PATH)

    migrations_parser = subparsers.add_parser("migrations", help="스키마 마이그레이션 상태 확인")
    migrations_parser.add_argument("--database", type=Path, default=DB_PATH)

    args = parser.parse_args()
    if args.command == "backup":
        result = backup_database(args.source, args.output, overwrite=args.force)
    elif args.command == "restore":
        result = restore_database(args.source, args.target, overwrite=args.force)
    elif args.command == "integrity":
        result = integrity_check(args.database)
    else:
        result = migration_status(args.database)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
