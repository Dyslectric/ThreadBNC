from __future__ import annotations

from .base import (
    REDDIT_DOMAIN,
    RSS_DOMAIN,
    RSS_PREFIX,
    CommentList,
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
    is_reddit_host,
    is_rss,
    parse_community_ref,
    parse_thread_url,
)
from .http import HttpClient
from .lemmy import LemmyAdapter
from .lemmy1 import JoinRequest, Lemmy1Adapter, speaks_v4
from .piefed import PieFedAdapter

ADAPTERS: dict[str, type[LemmyAdapter]] = {"lemmy": LemmyAdapter, "piefed": PieFedAdapter}


def adapter_class(software: str | None, version: str | None) -> type[LemmyAdapter] | None:
    """The adapter for a server, by NodeInfo software name and version."""
    if software == "lemmy" and speaks_v4(version):
        return Lemmy1Adapter
    return ADAPTERS.get(software or "")


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
    "ADAPTERS", "REDDIT_DOMAIN", "RSS_DOMAIN", "RSS_PREFIX", "CommentList", "CommunityRef", "HttpClient", "JoinRequest", "Lemmy1Adapter",
    "LemmyAdapter", "ModAction",
    "NActor", "NComment",
    "NCommunity", "NPost", "PieFedAdapter", "RemoteAuthError", "RemoteError", "RemoteNotFound",
    "RemoteRejected", "RemoteUnavailable",
    "ThreadRef", "ThreadiverseAdapter", "UnsupportedSoftware", "adapter_class", "detect_software", "host_of",
    "is_reddit_host", "is_rss", "parse_community_ref", "parse_thread_url",
]
