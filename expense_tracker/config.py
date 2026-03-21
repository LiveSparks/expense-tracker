from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _read_secret_file(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value).expanduser()


def _parse_csv(value: str | None, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    items = tuple(part.strip() for part in value.split(",") if part.strip())
    return items or default


def _secret_from_sources(
    *,
    direct_value: str | None,
    file_path: Path | None,
    legacy_file_path: Path | None = None,
) -> str | None:
    if direct_value and direct_value.strip():
        return direct_value.strip()
    file_value = _read_secret_file(file_path)
    if file_value:
        return file_value
    return _read_secret_file(legacy_file_path)


def write_secret_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.strip() + "\n", encoding="utf-8")
    path.chmod(0o600)


def generate_auth_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


@dataclass(frozen=True, slots=True)
class AppConfig:
    openai_api_key: str | None = None
    openai_api_key_file: Path | None = None
    auth_token: str | None = None
    auth_token_file: Path | None = None
    secure_cookies: bool = False
    cookie_name: str = "expense_tracker_auth"
    allowed_hosts: tuple[str, ...] = ("*",)

    @property
    def resolved_openai_api_key(self) -> str | None:
        return _secret_from_sources(
            direct_value=self.openai_api_key,
            file_path=self.openai_api_key_file,
            legacy_file_path=Path("/root/openai.key"),
        )

    @property
    def resolved_auth_token(self) -> str | None:
        return _secret_from_sources(
            direct_value=self.auth_token,
            file_path=self.auth_token_file,
        )

    @property
    def auth_enabled(self) -> bool:
        return bool(self.resolved_auth_token)


def load_app_config(environ: Mapping[str, str] | None = None) -> AppConfig:
    env = environ or os.environ
    return AppConfig(
        openai_api_key=env.get("EXPENSE_TRACKER_OPENAI_API_KEY"),
        openai_api_key_file=_parse_path(env.get("EXPENSE_TRACKER_OPENAI_API_KEY_FILE")),
        auth_token=env.get("EXPENSE_TRACKER_AUTH_TOKEN"),
        auth_token_file=_parse_path(env.get("EXPENSE_TRACKER_AUTH_TOKEN_FILE")),
        secure_cookies=_parse_bool(env.get("EXPENSE_TRACKER_SECURE_COOKIES"), default=False),
        cookie_name=(env.get("EXPENSE_TRACKER_COOKIE_NAME") or "expense_tracker_auth").strip() or "expense_tracker_auth",
        allowed_hosts=_parse_csv(env.get("EXPENSE_TRACKER_ALLOWED_HOSTS"), default=("*",)),
    )
