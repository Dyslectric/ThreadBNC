from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


REDDIT_MIN_POLL_MINUTES = 10  # the fastest a subreddit may be checked, whatever is asked for
RSS_MIN_POLL_MINUTES = 5  # likewise for a feed


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _relays(raw: str) -> dict[str, str]:
    """"dyslectric.dev=http://lemmy-dyslectric:8536, ..." -> {domain: Lemmy's address}."""
    out = {}
    for part in raw.split(","):
        domain, sep, upstream = part.partition("=")
        if sep and domain.strip() and upstream.strip():
            out[domain.strip().lower()] = upstream.strip().rstrip("/")
    return out


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
    # Server-wide default for shrinking files over media_max_bytes with ffmpeg
    # (each community can choose for itself), and the most downloaded to try.
    media_transcode: bool = False
    media_transcode_source_max_bytes: int = 1_000_000_000
    # Read the web pages posts link to and keep the article text (articles.py).
    archive_articles: bool = True
    default_trash_days: int | None = 30
    credentials_key: str | None = None
    # Signed in by a reverse proxy (e.g. Traefik + Authentik forward auth): the
    # proxy puts the user's name in `proxy_auth_header` and `proxy_secret` in
    # PROXY_SECRET_HEADER; only requests with both count. See web.py's guard.
    proxy_auth_header: str | None = None
    proxy_secret: str | None = None
    proxy_allowed_users: tuple[str, ...] = ()
    proxy_logout_url: str | None = None
    # Reddit: slower defaults than Lemmy/PieFed, since one app's request budget
    # (about 100 a minute) covers everything. See reddit.py.
    reddit_poll_minutes: int = 60
    reddit_min_request_interval: float = 2.0
    rss_poll_minutes: int = 60  # feeds rarely change faster, and conditional requests keep checks cheap
    # Your own Lemmy servers whose inboxes are routed through ThreadBNC, so what
    # they receive is pushed to it too (federation.py): domain -> Lemmy's own address.
    relay_inboxes: dict[str, str] = field(default_factory=dict)
    inbox_poll_minutes: int = 5  # replies, mentions and messages; Reddit's at least REDDIT_MIN_POLL_MINUTES


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
        media_transcode=_env_bool("THREADBNC_MEDIA_TRANSCODE", False),
        media_transcode_source_max_bytes=_env_int("THREADBNC_MEDIA_TRANSCODE_SOURCE_MAX_MB", 1000) * 1_000_000,
        archive_articles=_env_bool("THREADBNC_ARTICLES", True),
        default_trash_days=None if trash.lower() in ("", "none", "forever") else int(trash),
        credentials_key=os.environ.get("THREADBNC_CREDENTIALS_KEY") or None,
        proxy_auth_header=os.environ.get("THREADBNC_PROXY_AUTH_HEADER", "").strip() or None,
        proxy_secret=os.environ.get("THREADBNC_PROXY_SECRET") or None,
        proxy_allowed_users=tuple(u.strip().lower() for u in os.environ.get("THREADBNC_PROXY_ALLOWED_USERS", "")
                                  .split(",") if u.strip()),
        proxy_logout_url=os.environ.get("THREADBNC_PROXY_LOGOUT_URL", "/outpost.goauthentik.io/sign_out").strip()
        or None,
        reddit_poll_minutes=max(REDDIT_MIN_POLL_MINUTES, _env_int("THREADBNC_REDDIT_POLL_MINUTES", 60)),
        reddit_min_request_interval=max(1.0, float(os.environ.get("THREADBNC_REDDIT_MIN_REQUEST_INTERVAL", "2.0"))),
        rss_poll_minutes=max(RSS_MIN_POLL_MINUTES, _env_int("THREADBNC_RSS_POLL_MINUTES", 60)),
        inbox_poll_minutes=max(1, _env_int("THREADBNC_INBOX_POLL_MINUTES", 5)),
        relay_inboxes=_relays(os.environ.get("THREADBNC_RELAY_INBOXES", "")),
    )
