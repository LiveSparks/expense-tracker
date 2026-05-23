"""Scheduled database backup functionality."""
from __future__ import annotations

import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .migrations import apply_migrations
from .sqlite_utils import sqlite_connection


class BackupScheduler:
    def __init__(self, data_file: Path) -> None:
        self.data_file = data_file
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        apply_migrations(self.data_file)

    def get_settings(self) -> dict[str, object]:
        with sqlite_connection(self.data_file) as conn:
            row = conn.execute("SELECT * FROM backup_settings WHERE id = 1").fetchone()
            if row is None:
                return {"enabled": False, "interval_hours": 24, "retention_count": 7, "backup_dir": ""}
            return {
                "enabled": bool(row["enabled"]),
                "interval_hours": row["interval_hours"],
                "retention_count": row["retention_count"],
                "backup_dir": row["backup_dir"],
            }

    def save_settings(self, *, enabled: bool, interval_hours: int, retention_count: int, backup_dir: str) -> None:
        with sqlite_connection(self.data_file) as conn:
            conn.execute(
                """
                INSERT INTO backup_settings (id, enabled, interval_hours, retention_count, backup_dir)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    enabled=excluded.enabled,
                    interval_hours=excluded.interval_hours,
                    retention_count=excluded.retention_count,
                    backup_dir=excluded.backup_dir
                """,
                (int(enabled), interval_hours, retention_count, backup_dir),
            )

    def list_runs(self, limit: int = 20) -> list[dict[str, object]]:
        with sqlite_connection(self.data_file) as conn:
            rows = conn.execute(
                "SELECT * FROM backup_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def run_backup(self) -> dict[str, str]:
        run_id = str(uuid4())
        started_at = datetime.now(UTC).isoformat()
        settings = self.get_settings()
        backup_dir_value = str(settings["backup_dir"])
        backup_dir = Path(backup_dir_value) if backup_dir_value else self.data_file.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        dest = backup_dir / f"ledger-backup-{timestamp}.db"
        try:
            shutil.copy2(self.data_file, dest)
            size = dest.stat().st_size
            completed_at = datetime.now(UTC).isoformat()
            self._prune_backups(backup_dir, int(settings["retention_count"]))
            with sqlite_connection(self.data_file) as conn:
                conn.execute(
                    "INSERT INTO backup_runs (id, started_at, completed_at, status, file_path, file_size_bytes) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, started_at, completed_at, "success", str(dest), size),
                )
            return {"id": run_id, "status": "success", "file_path": str(dest)}
        except Exception as exc:  # pragma: no cover - exercised by runtime failures
            completed_at = datetime.now(UTC).isoformat()
            with sqlite_connection(self.data_file) as conn:
                conn.execute(
                    "INSERT INTO backup_runs (id, started_at, completed_at, status, error_message) VALUES (?, ?, ?, ?, ?)",
                    (run_id, started_at, completed_at, "error", str(exc)),
                )
            return {"id": run_id, "status": "error", "error": str(exc)}

    def _prune_backups(self, backup_dir: Path, retention_count: int) -> None:
        if retention_count <= 0:
            return
        backups = sorted(backup_dir.glob("ledger-backup-*.db"))
        while len(backups) > retention_count:
            backups.pop(0).unlink(missing_ok=True)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="backup-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run_loop(self) -> None:
        settings: dict[str, object] = self.get_settings()
        while not self._stop_event.is_set():
            try:
                settings = self.get_settings()
                if settings["enabled"] and self.data_file.exists():
                    self.run_backup()
            except Exception:
                pass
            interval = max(1, int(settings.get("interval_hours", 24))) * 3600
            self._stop_event.wait(interval)
