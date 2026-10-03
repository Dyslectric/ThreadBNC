"""Settings, from the environment (PIEFRONT_*)."""

from __future__ import annotations

import logging
import os
import re
import secrets
from dataclasses import dataclass

from . import __version__

log = logging.getLogger("piefront.config")

_HOST = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?")


@dataclass(frozen=True)
class Settings:
    server: str  # the PieFed server this is a frontend for: its host name
    secret_key: str  # signs session cookies
    name: str | None = None  # what the site is called here; the server's own name when not set
    https_only_cookies: bool = True
    user_agent: str = f"piefront/{__version__}"
    cache_seconds: int = 30  # how long a page read as nobody is reused for everyone not signed in
    forum_cache_seconds: int = 1800  # the server's topics and feeds: a big list, seldom changed


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None or not value.strip() else value.strip().lower() not in ("0", "false", "no", "off")


def load_settings() -> Settings:
    server = os.environ.get("PIEFRONT_SERVER", "").strip().lower()
    server = server.removeprefix("https://").removeprefix("http://").strip("/")
    if not _HOST.fullmatch(server):
        raise SystemExit("Set PIEFRONT_SERVER to the PieFed server this is a frontend for, e.g. piefed.social")
    secret = os.environ.get("PIEFRONT_SECRET_KEY", "").strip()
    if not secret:
        secret = secrets.token_hex(32)
        log.warning("PIEFRONT_SECRET_KEY isn't set: using one made up for this run, so everyone is "
                    "signed out when it restarts.")
    return Settings(
        server=server, secret_key=secret, name=os.environ.get("PIEFRONT_NAME", "").strip() or None,
        https_only_cookies=_flag("PIEFRONT_HTTPS_ONLY", True),
        user_agent=os.environ.get("PIEFRONT_USER_AGENT", "").strip() or f"piefront/{__version__} (frontend for {server})",
        cache_seconds=max(0, int(os.environ.get("PIEFRONT_CACHE_SECONDS", "30") or 30)),
    )
