"""Private communities (Lemmy 1.0): who may join.

A private community's posts and comments are only shown to followers its
moderators have approved. ThreadBNC keeps an approved list of usernames per
community and, about once a minute, approves join requests from anyone on it,
acting through one of your moderator accounts on the community's own server.
Everyone else waits for you to approve or deny them by hand.

Lemmy can't take back an approval, so taking someone off the list only revokes
their access if you ask for it, by banning them from the community. Putting
them back on the list lifts that ban; they then need to ask to join again.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from .accounts import Account, AccountError, Poster
from .adapters import JoinRequest, host_of
from .db import parse_ts, utcnow
from .moderation import Moderation

SWEEP_EVERY = timedelta(seconds=60)
VISIBILITIES = {"public": "Public", "private": "Private (members only)"}
REVOKE_REASON = "Removed from the community's members list"
_HANDLE = re.compile(r"^[a-z0-9_.\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)+(:\d+)?$")


def handle_of(text: str) -> str:
    """Normalise user@host, @user@host or a profile URL to user@host."""
    text = text.strip()
    if text.startswith(("http://", "https://")):
        u = urlparse(text)
        name = u.path.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
        text = f"{name}@{u.netloc}"
    handle = text.lstrip("@").lower()
    if not _HANDLE.match(handle):
        raise AccountError(f"“{text}” isn't a username; use the form user@instance.")
    return handle


def request_handle(req: JoinRequest) -> str:
    return f"{req.person.username}@{req.person.domain}".lower()


def attach(bouncer: Any, settings: Any) -> "PrivateCommunities":
    """Account-backed tools for a bouncer running on its own (``python -m
    threadbnc bouncer``), with the join-request sweep hooked into its loop."""
    from .vault import TokenVault

    private = PrivateCommunities(Moderation(Poster(bouncer, TokenVault(settings.credentials_key,
                                                                        settings.data_dir))))
    bouncer.hooks.append(private.sweep)
    bouncer.read_token = private.read_token
    return private


class PrivateCommunities:
    def __init__(self, mod: Moderation):
        self.mod = mod
        self.poster: Poster = mod.poster
        self.db = mod.db

    # -- checks ----------------------------------------------------------------
    def _community(self, community_id: int) -> Any:
        with self.db.connect() as conn:
            c = conn.execute("SELECT * FROM communities WHERE id=?", (community_id,)).fetchone()
        if c is None:
            raise AccountError("Community not found.")
        return c

    def _require_manager(self, account: Account, community_id: int) -> Any:
        """The account must moderate the community from the community's own
        server (that's where join requests arrive), and it must run Lemmy 1.0+."""
        self.mod._require(account, community_id)
        c = self._community(community_id)
        home = host_of(c["canonical_ap_id"])
        if account.domain != home:
            raise AccountError(f"Join requests are handled on {home}. Act as a moderator account from {home} "
                               "(switch accounts in the header).")
        adapter = self.poster.bouncer.adapter_for(home)
        if not getattr(adapter, "supports_private_communities", False):
            raise AccountError(f"{home} doesn't support private communities; they need Lemmy 1.0 or later.")
        return c

    def supported(self, community_id: int) -> bool:
        try:
            home = host_of(self._community(community_id)["canonical_ap_id"])
            return bool(getattr(self.poster.bouncer.adapter_for(home), "supports_private_communities", False))
        except Exception:  # unknown/unreachable server: just don't offer it
            return False

    def _manage(self, conn: Any, community_id: int, account: Account, visibility: str | None = None) -> None:
        conn.execute(
            "INSERT INTO private_communities(community_id, account_id, visibility, active) VALUES (?,?,?,?) "
            "ON CONFLICT(community_id) DO UPDATE SET account_id=excluded.account_id, "
            "visibility=COALESCE(excluded.visibility, private_communities.visibility), "
            "active=CASE WHEN COALESCE(excluded.visibility, private_communities.visibility, 'private')='private' "
            "THEN 1 ELSE 0 END",
            (community_id, account.id, visibility, 1 if (visibility or "private") == "private" else 0))

    # -- what the Mod tab shows ------------------------------------------------------
    def overview(self, account: Account | None, community_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            managed = conn.execute("SELECT p.*, a.username || '@' || a.domain AS account_handle "
                                   "FROM private_communities p LEFT JOIN accounts a ON a.id=p.account_id "
                                   "WHERE p.community_id=?", (community_id,)).fetchone()
            members = [r["handle"] for r in conn.execute(
                "SELECT handle FROM private_members WHERE community_id=? ORDER BY handle", (community_id,))]
            waiting = conn.execute("SELECT * FROM join_requests WHERE community_id=? AND status='waiting' "
                                   "ORDER BY first_seen_at", (community_id,)).fetchall()
            decided = conn.execute("SELECT * FROM join_requests WHERE community_id=? AND status!='waiting' "
                                   "ORDER BY decided_at DESC LIMIT 30", (community_id,)).fetchall()
        visibility, error = (managed["visibility"] if managed else None), None
        if account is not None:
            try:
                visibility = self._live_visibility(account, community_id) or visibility
            except AccountError as exc:
                error = str(exc)
        return {"managed": managed, "members": members, "waiting": waiting, "decided": decided,
                "visibility": visibility, "error": error, "choices": VISIBILITIES}

    def _live_visibility(self, account: Account, community_id: int) -> str | None:
        c = self._require_manager(account, community_id)

        def act(adapter: Any, token: str) -> str | None:
            local = adapter.resolve_as(token, c["canonical_ap_id"])["community"]
            return adapter.community_by_id(token, local).visibility
        visibility = self.poster._run(account, act)
        with self.db.transaction() as conn:
            conn.execute("UPDATE private_communities SET visibility=?, active=? WHERE community_id=?",
                         (visibility, 1 if visibility == "private" else 0, community_id))
        return visibility

    # -- actions -----------------------------------------------------------------------
    def set_visibility(self, account: Account, community_id: int, visibility: str) -> None:
        if visibility not in VISIBILITIES:
            raise AccountError("Choose public or private.")
        c = self._require_manager(account, community_id)

        def act(adapter: Any, token: str) -> None:
            local = adapter.resolve_as(token, c["canonical_ap_id"])["community"]
            adapter.set_community_visibility(token, local, visibility)
        self.poster._run(account, act)
        with self.db.transaction() as conn:
            self._manage(conn, community_id, account, visibility)
        self.mod._log(account, f"visibility_{visibility}", community_id=community_id)
        if visibility == "private":
            self.sweep_community(community_id)

    def add_member(self, account: Account, community_id: int, who: str) -> str:
        handle = handle_of(who)
        self._require_manager(account, community_id)
        now = utcnow()
        with self.db.transaction() as conn:
            self._manage(conn, community_id, account)
            conn.execute("INSERT INTO private_members(community_id, handle, added_at) VALUES (?,?,?) "
                         "ON CONFLICT DO NOTHING", (community_id, handle, now))
            revoked = conn.execute("SELECT id FROM join_requests WHERE community_id=? AND handle=? "
                                   "AND status='revoked'", (community_id, handle)).fetchone()
        self.mod._log(account, "member_add", community_id=community_id, target=handle)
        if revoked:  # we banned them when they were taken off the list: lift it
            self.mod.community_ban(account, community_id, handle, ban=False)
            with self.db.transaction() as conn:
                conn.execute("UPDATE join_requests SET status='unbanned', decided_by='you', decided_at=? "
                             "WHERE id=?", (utcnow(), revoked["id"]))
        self.sweep_community(community_id)  # approve them now if they're already waiting
        return handle

    def remove_member(self, account: Account, community_id: int, who: str, revoke: bool) -> str:
        handle = handle_of(who)
        self._require_manager(account, community_id)
        if revoke:
            self.mod.community_ban(account, community_id, handle, ban=True, reason=REVOKE_REASON)
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM private_members WHERE community_id=? AND handle=?", (community_id, handle))
            if revoke:
                self._record(conn, community_id, handle, None, "revoked", "you", now)
        self.mod._log(account, "member_remove", community_id=community_id, target=handle)
        return handle

    def answer(self, account: Account, community_id: int, request_id: int, approve: bool,
               add_to_list: bool = False) -> str:
        self._require_manager(account, community_id)
        with self.db.connect() as conn:
            req = conn.execute("SELECT * FROM join_requests WHERE id=? AND community_id=?",
                               (request_id, community_id)).fetchone()
        if req is None or req["status"] != "waiting":
            raise AccountError("That request has already been answered.")
        self.poster._run(account, lambda adapter, token: adapter.answer_join_request(
            token, req["community_local_id"], req["person_local_id"], approve))
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute("UPDATE join_requests SET status=?, decided_by='you', decided_at=? WHERE id=?",
                         ("approved" if approve else "denied", now, request_id))
            if approve and add_to_list:
                conn.execute("INSERT INTO private_members(community_id, handle, added_at) VALUES (?,?,?) "
                             "ON CONFLICT DO NOTHING", (community_id, req["handle"], now))
        self.mod._log(account, "join_approve" if approve else "join_deny", community_id=community_id,
                      target=req["handle"])
        return req["handle"]

    # -- reading as a member -------------------------------------------------------------
    def read_token(self, domain: str, community_id: int) -> str | None:
        """The managing account's session, so the bouncer can archive a private
        community (only members can read it). Only for that community, and only
        on the account's own server."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT account_id FROM private_communities WHERE community_id=? AND active=1",
                               (community_id,)).fetchone()
        account = self.poster.get(row["account_id"]) if row else None
        if account is None or account.domain != domain or account.status != "ok":
            return None
        try:
            return self.poster._session(account)[1]
        except Exception:  # can't decrypt: the sweep reports it; read anonymously meanwhile
            return None

    # -- the sweep -----------------------------------------------------------------------
    def sweep(self, force: bool = False) -> int:
        """Pick up new join requests and approve listed usernames. Called from
        the bouncer's loop; does nothing more than once a minute. Returns the
        number of requests approved automatically."""
        now = utcnow()
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM private_communities WHERE active=1").fetchall()
        due = [r for r in rows if force or not r["last_swept_at"]
               or parse_ts(now) - parse_ts(r["last_swept_at"]) >= SWEEP_EVERY]  # type: ignore[operator]
        by_account: dict[int | None, list[Any]] = {}
        for r in due:
            by_account.setdefault(r["account_id"], []).append(r)
        return sum(self._sweep_account(aid, group) for aid, group in by_account.items())

    def sweep_community(self, community_id: int) -> int:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM private_communities WHERE community_id=? AND active=1",
                               (community_id,)).fetchone()
        return self._sweep_account(row["account_id"], [row]) if row else 0

    def _sweep_account(self, account_id: int | None, rows: list[Any]) -> int:
        """One request list per account covers every community it moderates."""
        account = self.poster.get(account_id)
        if account is None:
            self._note(rows, "The account that managed this community was removed; set it up again from "
                             "the Mod tab.")
            return 0
        try:
            requests = self.poster._run(account, lambda adapter, token: adapter.join_requests(token))
        except AccountError as exc:
            self._note(rows, str(exc))
            return 0
        approved = 0
        for row in rows:
            c = self._community(row["community_id"])
            mine = [r for r in requests if r.community_ap_id == c["canonical_ap_id"]]
            try:
                approved += self._apply(account, row["community_id"], mine)
                self._note([row], None)
            except AccountError as exc:
                self._note([row], str(exc))
        return approved

    def _apply(self, account: Account, community_id: int, requests: list[JoinRequest]) -> int:
        now = utcnow()
        with self.db.transaction() as conn:
            members = {r["handle"] for r in conn.execute(
                "SELECT handle FROM private_members WHERE community_id=?", (community_id,))}
            for req in requests:
                self._record(conn, community_id, request_handle(req), req, "waiting", None, now)
            # Waiting here but no longer pending there: answered in Lemmy's own UI.
            seen = {request_handle(r) for r in requests}
            for r in conn.execute("SELECT id, handle FROM join_requests WHERE community_id=? AND status='waiting'",
                                  (community_id,)).fetchall():
                if r["handle"] not in seen:
                    conn.execute("UPDATE join_requests SET status='elsewhere', decided_at=? WHERE id=?",
                                 (now, r["id"]))
        approved = 0
        for req in requests:
            handle = request_handle(req)
            if handle not in members:
                continue
            self.poster._run(account, lambda adapter, token: adapter.answer_join_request(
                token, req.community_local_id, req.person_local_id, True))
            with self.db.transaction() as conn:
                conn.execute("UPDATE join_requests SET status='approved', decided_by='list', decided_at=? "
                             "WHERE community_id=? AND handle=?", (utcnow(), community_id, handle))
            self.mod._log(account, "join_approve_listed", community_id=community_id, target=req.person)
            approved += 1
        return approved

    @staticmethod
    def _record(conn: Any, community_id: int, handle: str, req: JoinRequest | None, status: str,
                decided_by: str | None, now: str) -> None:
        """Create or update the request row for `handle`. A fresh request from
        someone answered before (or revoked) starts waiting again."""
        conn.execute(
            "INSERT INTO join_requests(community_id, handle, person_ap_id, display_name, person_local_id, "
            "community_local_id, status, decided_by, first_seen_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(community_id, handle) DO UPDATE SET "
            "person_ap_id=COALESCE(excluded.person_ap_id, join_requests.person_ap_id), "
            "display_name=COALESCE(excluded.display_name, join_requests.display_name), "
            "person_local_id=COALESCE(excluded.person_local_id, join_requests.person_local_id), "
            "community_local_id=COALESCE(excluded.community_local_id, join_requests.community_local_id), "
            "first_seen_at=CASE WHEN join_requests.status='waiting' THEN join_requests.first_seen_at "
            "ELSE excluded.first_seen_at END, "
            "status=excluded.status, decided_by=excluded.decided_by, decided_at=excluded.decided_at",
            (community_id, handle, req.person.ap_id if req else None, req.person.display_name if req else None,
             req.person_local_id if req else None, req.community_local_id if req else None, status, decided_by,
             now, None if status == "waiting" else now))

    def _note(self, rows: list[Any], error: str | None) -> None:
        with self.db.transaction() as conn:
            for r in rows:
                conn.execute("UPDATE private_communities SET last_swept_at=?, last_error=? WHERE community_id=?",
                             (utcnow(), error, r["community_id"]))
