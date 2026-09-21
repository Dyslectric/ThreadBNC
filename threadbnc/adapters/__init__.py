from __future__ import annotations

from .base import (
    CommunityRef,
    ModAction,
    NActor,
    NComment,
    NCommunity,
    NPost,
    RemoteAuthError,
    RemoteError,
    RemoteNotFound,
    RemoteRejected,
    RemoteUnavailable,
    ThreadiverseAdapter,
    ThreadRef,
    UnsupportedSoftware,
    host_of,
    parse_community_ref,
    parse_thread_url,
)
from .http import HttpClient
from .lemmy import LemmyAdapter
from .piefed import PieFedAdapter

ADAPTERS: dict[str, type[LemmyAdapter]] = {"lemmy": LemmyAdapter, "piefed": PieFedAdapter}


def detect_software(http: HttpClient, domain: str) -> tuple[str, str | None]:
    """Use NodeInfo to identify server software. Returns (name, version)."""
    index = http.get_json(domain, "/.well-known/nodeinfo")
    links = index.get("links") or []
    href = None
    for link in sorted(links, key=lambda x: x.get("rel", ""), reverse=True):
        if "nodeinfo" in link.get("rel", ""):
            href = link.get("href")
            break
    if not href:
        raise UnsupportedSoftware(f"{domain}: no nodeinfo link")
    from urllib.parse import urlparse

    path = urlparse(href).path
    info = http.get_json(domain, path)
    software = info.get("software") or {}
    return (software.get("name") or "unknown").lower(), software.get("version")


__all__ = [
    "ADAPTERS", "CommunityRef", "HttpClient", "LemmyAdapter", "ModAction", "NActor", "NComment",
    "NCommunity", "NPost", "PieFedAdapter", "RemoteAuthError", "RemoteError", "RemoteNotFound",
    "RemoteRejected", "RemoteUnavailable",
    "ThreadRef", "ThreadiverseAdapter", "UnsupportedSoftware", "detect_software", "host_of",
    "parse_community_ref", "parse_thread_url",
]
