"""Versioned SQLite migration system.

Each migration is identified by an integer version number. Migrations are
applied in ascending order. The current version is stored in the
``schema_migrations`` table.

Usage::

    from .migrations import apply_migrations
    apply_migrations(data_file)

New migrations should be appended to the :data:`MIGRATIONS` list. Each entry
is a ``(version: int, sql: str)`` pair. The SQL is executed inside a
``BEGIN ... COMMIT`` transaction so partial migrations are automatically rolled
back on error.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

from .sqlite_utils import sqlite_connection


# ---------------------------------------------------------------------------
# Migration definitions
# ---------------------------------------------------------------------------
# Each entry: (version: int, sql: str)
# Version numbers must be unique, positive, and ideally contiguous.
# ---------------------------------------------------------------------------

MIGRATIONS: Sequence[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS sms_log (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            review_id TEXT,
            sender TEXT NOT NULL,
            content_excerpt TEXT NOT NULL,
            event_details_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sms_log_created_at ON sms_log(created_at);
        CREATE INDEX IF NOT EXISTS idx_sms_log_event_type ON sms_log(event_type);
        CREATE INDEX IF NOT EXISTS idx_sms_log_review_id ON sms_log(review_id);
        """,
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_migrations(data_file: Path) -> list[int]:
    """Apply all pending migrations to *data_file* and return applied versions.

    This function is idempotent: calling it multiple times on an up-to-date
    database is a no-op.
    """
    data_file.parent.mkdir(parents=True, exist_ok=True)
    with sqlite_connection(data_file) as connection:
        _ensure_migrations_table(connection)
        applied = _applied_versions(connection)
        newly_applied: list[int] = []
        for version, sql in sorted(MIGRATIONS, key=lambda m: m[0]):
            if version in applied:
                continue
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (version,),
            )
            newly_applied.append(version)
        return newly_applied


def current_version(data_file: Path) -> int:
    """Return the highest applied migration version, or 0 if none applied."""
    if not data_file.exists():
        return 0
    try:
        with sqlite_connection(data_file) as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if row is None:
                return 0
            result = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            return int(result[0]) if result else 0
    except sqlite3.Error:
        return 0


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _ensure_migrations_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY
        )
        """
    )


def _applied_versions(connection: sqlite3.Connection) -> set[int]:
    rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(row[0]) for row in rows}
