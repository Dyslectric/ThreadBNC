from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: Path
    db_path: Path
    database_url: str | None
    password: str | None
    api_token: str | None
    secret_key: str
    https_only_cookies: bool
    embedded_bouncer: bool
    default_sync_minutes: int
    default_follow_poll_minutes: int
    default_follow_retention_days: int | None
    http_timeout: float
    min_request_interval: float
    user_agent: str
    media_dir: Path | None = None
    media_max_bytes: int = 25_000_000
    default_trash_days: int | None = 30


def _load_secret(data_dir: Path) -> str:
    env = os.environ.get("THREADBNC_SECRET_KEY")
    if env:
        return env
    path = data_dir / "secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    return key


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("THREADBNC_DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    retention = os.environ.get("THREADBNC_FOLLOW_RETENTION_DAYS", "30")
    trash = os.environ.get("THREADBNC_TRASH_DAYS", "30")
    return Settings(
        data_dir=data_dir,
        db_path=Path(os.environ.get("THREADBNC_DB", str(data_dir / "archive.sqlite3"))),
        database_url=os.environ.get("THREADBNC_DATABASE_URL") or None,
        password=os.environ.get("THREADBNC_PASSWORD") or None,
        api_token=os.environ.get("THREADBNC_API_TOKEN") or None,
        secret_key=_load_secret(data_dir),
        https_only_cookies=_env_bool("THREADBNC_HTTPS_ONLY", False),
        embedded_bouncer=_env_bool("THREADBNC_EMBEDDED_BOUNCER", True),
        default_sync_minutes=_env_int("THREADBNC_SYNC_MINUTES", 30),
        default_follow_poll_minutes=_env_int("THREADBNC_FOLLOW_POLL_MINUTES", 15),
        default_follow_retention_days=None if retention.lower() in ("", "none", "forever") else int(retention),
        http_timeout=float(os.environ.get("THREADBNC_HTTP_TIMEOUT", "20")),
        min_request_interval=float(os.environ.get("THREADBNC_MIN_REQUEST_INTERVAL", "1.0")),
        user_agent=os.environ.get(
            "THREADBNC_USER_AGENT", "ThreadBNC/0.1 (private thread archive; polling bouncer)"
        ),
        media_dir=Path(os.environ.get("THREADBNC_MEDIA_DIR", str(data_dir / "media"))),
        media_max_bytes=_env_int("THREADBNC_MEDIA_MAX_MB", 25) * 1_000_000,
        default_trash_days=None if trash.lower() in ("", "none", "forever") else int(trash),
    )
