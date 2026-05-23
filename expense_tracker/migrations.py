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
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
        """,
    ),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            key_prefix TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
        """,
    ),
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS backup_settings (
            id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            interval_hours INTEGER NOT NULL DEFAULT 24,
            retention_count INTEGER NOT NULL DEFAULT 7,
            backup_dir TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS backup_runs (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            file_path TEXT,
            file_size_bytes INTEGER,
            error_message TEXT
        );
        """,
    ),
    (
        5,
        """
        ALTER TABLE transactions ADD COLUMN verified INTEGER NOT NULL DEFAULT 0;
        """,
    ),
]


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
            if version == 5:
                _ensure_core_tables(connection)
                if _column_exists(connection, "transactions", "verified"):
                    connection.execute(
                        "INSERT INTO schema_migrations (version) VALUES (?)",
                        (version,),
                    )
                    newly_applied.append(version)
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


def _column_exists(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)


def _ensure_core_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payees (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            linked_account_id TEXT REFERENCES accounts(id)
        );
        CREATE TABLE IF NOT EXISTS categories (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subcategories (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category_id TEXT NOT NULL REFERENCES categories(id)
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            entry_type TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            payee_id TEXT NOT NULL REFERENCES payees(id),
            category_id TEXT NOT NULL REFERENCES categories(id),
            subcategory_id TEXT REFERENCES subcategories(id),
            amount TEXT NOT NULL,
            notes TEXT NOT NULL,
            spent_on TEXT NOT NULL,
            created_at TEXT NOT NULL,
            linked_transaction_id TEXT,
            transfer_group_id TEXT,
            verified INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY,
            transaction_id TEXT NOT NULL REFERENCES transactions(id),
            original_name TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            uploaded_at TEXT NOT NULL
        );
        """
    )
