from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .migrations import apply_migrations
from .sqlite_utils import sqlite_connection

_PASSWORD_ITERATIONS = 260000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS)
    return f"pbkdf2:sha256:{_PASSWORD_ITERATIONS}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, digest_name, iterations_text, salt_hex, hash_hex = stored_hash.split(":", maxsplit=4)
        if algorithm != "pbkdf2" or digest_name != "sha256":
            return False
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserStore:
    def __init__(self, data_file: Path) -> None:
        self.data_file = data_file
        apply_migrations(self.data_file)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def user_count(self) -> int:
        with sqlite_connection(self.data_file) as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
            return int(row["count"]) if row else 0

    def create_user(self, username: str, password: str) -> str:
        normalized_username = username.strip()
        if not normalized_username:
            raise ValueError("Username is required.")
        if not password:
            raise ValueError("Password is required.")
        user_id = str(uuid4())
        created_at = self._now().isoformat()
        with sqlite_connection(self.data_file) as connection:
            existing = connection.execute(
                "SELECT id FROM users WHERE lower(username) = lower(?)",
                (normalized_username,),
            ).fetchone()
            if existing is not None:
                raise ValueError("Username already exists.")
            connection.execute(
                "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, normalized_username, hash_password(password), created_at),
            )
        return user_id

    def authenticate(self, username: str, password: str) -> str | None:
        normalized_username = username.strip()
        if not normalized_username or not password:
            return None
        with sqlite_connection(self.data_file) as connection:
            row = connection.execute(
                "SELECT id, password_hash FROM users WHERE lower(username) = lower(?)",
                (normalized_username,),
            ).fetchone()
            if row is None or not verify_password(password, str(row["password_hash"])):
                return None
            return str(row["id"])

    def create_session(self, user_id: str, expires_days: int = 90) -> str:
        raw_token = secrets.token_urlsafe(32)
        created_at = self._now()
        expires_at = (created_at + timedelta(days=expires_days)).isoformat() if expires_days else None
        with sqlite_connection(self.data_file) as connection:
            connection.execute(
                "INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid4()), user_id, hash_token(raw_token), created_at.isoformat(), expires_at),
            )
        return raw_token

    def validate_session(self, raw_token: str) -> str | None:
        if not raw_token:
            return None
        self._prune_expired_sessions()
        with sqlite_connection(self.data_file) as connection:
            row = connection.execute(
                "SELECT user_id FROM sessions WHERE token_hash = ?",
                (hash_token(raw_token),),
            ).fetchone()
            return str(row["user_id"]) if row is not None else None

    def delete_session(self, raw_token: str) -> None:
        if not raw_token:
            return
        with sqlite_connection(self.data_file) as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(raw_token),))

    def create_api_key(self, user_id: str, name: str) -> tuple[str, str]:
        key_name = name.strip()
        if not key_name:
            raise ValueError("API key name is required.")
        raw_key = f"et_{secrets.token_urlsafe(32)}"
        key_id = str(uuid4())
        with sqlite_connection(self.data_file) as connection:
            connection.execute(
                "INSERT INTO api_keys (id, user_id, name, key_hash, key_prefix, created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_id, user_id, key_name, hash_token(raw_key), raw_key[:12], self._now().isoformat(), None),
            )
        return raw_key, key_id

    def list_api_keys(self, user_id: str) -> list[dict[str, str | None]]:
        with sqlite_connection(self.data_file) as connection:
            rows = connection.execute(
                "SELECT id, name, key_prefix, created_at, last_used_at FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_api_key(self, key_id: str, user_id: str) -> bool:
        with sqlite_connection(self.data_file) as connection:
            cursor = connection.execute(
                "DELETE FROM api_keys WHERE id = ? AND user_id = ?",
                (key_id, user_id),
            )
            return cursor.rowcount > 0

    def validate_api_key(self, raw_key: str) -> str | None:
        if not raw_key:
            return None
        with sqlite_connection(self.data_file) as connection:
            row = connection.execute(
                "SELECT id, user_id FROM api_keys WHERE key_hash = ?",
                (hash_token(raw_key),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                (self._now().isoformat(), row["id"]),
            )
            return str(row["user_id"])

    def _prune_expired_sessions(self) -> None:
        with sqlite_connection(self.data_file) as connection:
            connection.execute(
                "DELETE FROM sessions WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (self._now().isoformat(),),
            )
