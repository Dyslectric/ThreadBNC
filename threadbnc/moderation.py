"""Moderator and admin tools, acting through a logged-in account.

Community-level (you moderate the community): add/remove moderators, ban or
unban people from it, remove/restore/lock/pin its posts and comments.
Server-level (you administer the account's server): ban people from the whole
server, block instances (defederate) and link domains, set registration mode.

Like your own deletions, actions on posts/comments are recorded in the archive
as *requests*; the resulting state is recorded when the bouncer observes it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import store
from .accounts import Account, AccountError, Poster
from .adapters import NActor, host_of
from .db import utcnow

REGISTRATION_MODES = {"Closed": "Closed (nobody can sign up)",
                      "RequireApplication": "Require application (you approve sign-ups)",
                      "Open": "Open (anyone can sign up)"}


@dataclass
class ModPowers:
    moderator: bool  # listed as a moderator of the community
    admin: bool      # admin of the community's home server

    @property
    def any(self) -> bool:
        return self.moderator or self.admin


def _clean_domain(text: str) -> str:
    text = text.strip().lower()
    if "://" in text:
        text = host_of(text)
    text = text.strip("/").split("/")[0]
    if not text or "." not in text or " " in text:
        raise AccountError("Enter a domain like spam.example")
    return text


class Moderation:
    def __init__(self, poster: Poster):
        self.poster = poster
        self.db = poster.db

    # -- who can do what -------------------------------------------------------
    def powers(self, account: Account | None, community_id: int) -> ModPowers:
        if account is None:
            return ModPowers(False, False)
        with self.db.connect() as conn:
            c = conn.execute("SELECT canonical_ap_id, moderators_json FROM communities WHERE id=?",
                             (community_id,)).fetchone()
        if c is None:
            return ModPowers(False, False)
        mods = set(json.loads(c["moderators_json"] or "[]"))
        return ModPowers(account.actor_ap_id in mods,
                         account.is_admin and host_of(c["canonical_ap_id"]) == account.domain)

    def _require(self, account: Account, community_id: int) -> None:
        if not self.powers(account, community_id).any:
            raise AccountError(f"{account.handle} isn't a moderator of that community.")

    # -- helpers -------------------------------------------------------------------
    def _community_local(self, adapter: Any, token: str, account: Account, community_id: int) -> str:
        with self.db.connect() as conn:
            c = conn.execute("SELECT canonical_ap_id FROM communities WHERE id=?", (community_id,)).fetchone()
        if c is None:
            raise AccountError("Community not found.")
        found = adapter.resolve_as(token, c["canonical_ap_id"])
        if "community" not in found:
            raise AccountError(f"{account.domain} couldn't find that community.")
        return found["community"]

    def _log(self, account: Account, action: str, *, community_id: int | None = None,
             target: NActor | str | None = None, reason: str | None = None, days: int | None = None) -> None:
        name = target if isinstance(target, str) or target is None else f"{target.username}@{target.domain}"
        ap_id = target.ap_id if isinstance(target, NActor) else None
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO mod_actions(account_id, account_handle, action, community_id, target, target_ap_id, "
                "reason, expires_days, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (account.id, account.handle, action, community_id, name, ap_id, reason or None, days, utcnow()))

    def _run(self, account: Account, fn: Any) -> Any:
        return self.poster._run(account, fn)  # remote errors come back as AccountError

    def recent_actions(self, *, community_id: int | None = None, account_id: int | None = None,
                       limit: int = 50) -> list[Any]:
        where, args = "1=1", []
        if community_id is not None:
            where, args = "community_id=?", [community_id]
        elif account_id is not None:
            where, args = "account_id=? AND community_id IS NULL", [account_id]
        with self.db.connect() as conn:
            return conn.execute(f"SELECT * FROM mod_actions WHERE {where} ORDER BY id DESC LIMIT ?",
                                [*args, limit]).fetchall()

    # -- community: moderators ---------------------------------------------------------
    def moderators(self, account: Account, community_id: int) -> list[NActor]:
        def act(adapter: Any, token: str) -> list[NActor]:
            return adapter.community_moderators(token, self._community_local(adapter, token, account, community_id))
        mods = self._run(account, act)
        self._store_moderators(community_id, mods)
        return mods

    def set_moderator(self, account: Account, community_id: int, who: str, added: bool) -> list[NActor]:
        self._require(account, community_id)

        def act(adapter: Any, token: str) -> tuple[NActor, list[NActor]]:
            cid = self._community_local(adapter, token, account, community_id)
            pid, person = adapter.resolve_person(token, who.strip())
            return person, adapter.set_moderator(token, cid, pid, added)

        person, mods = self._run(account, act)
        self._log(account, "add_moderator" if added else "remove_moderator", community_id=community_id,
                  target=person)
        self._store_moderators(community_id, mods)
        return mods

    def _store_moderators(self, community_id: int, mods: list[NActor]) -> None:
        with self.db.transaction() as conn:
            c = conn.execute("SELECT moderators_json FROM communities WHERE id=?", (community_id,)).fetchone()
            store.observe_moderators(conn, community_id, c["moderators_json"], [m.ap_id for m in mods], utcnow())

    # -- community: bans ------------------------------------------------------------
    def community_ban(self, account: Account, community_id: int, who: str, ban: bool = True,
                      reason: str | None = None, days: int | None = None, remove_content: bool = False) -> NActor:
        self._require(account, community_id)
        if ban and not (reason or "").strip():
            raise AccountError("Give a reason for the ban; it's shown in the modlog.")

        def act(adapter: Any, token: str) -> NActor:
            cid = self._community_local(adapter, token, account, community_id)
            pid, person = adapter.resolve_person(token, who.strip())
            adapter.ban_from_community(token, cid, pid, ban, (reason or "").strip() or None, days, remove_content)
            return person

        person = self._run(account, act)
        self._log(account, "community_ban" if ban else "community_unban", community_id=community_id,
                  target=person, reason=reason, days=days)
        return person

    def community_bans(self, account: Account, community_id: int) -> tuple[list[NActor] | None, list[Any]]:
        """(bans as reported by the server if it can list them, bans made via ThreadBNC)."""
        def act(adapter: Any, token: str) -> list[NActor] | None:
            return adapter.community_bans(token, self._community_local(adapter, token, account, community_id))
        try:
            server_list = self._run(account, act)
        except AccountError:
            server_list = None
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT m.* FROM mod_actions m WHERE m.community_id=? AND m.action='community_ban' AND NOT EXISTS "
                "(SELECT 1 FROM mod_actions u WHERE u.community_id=m.community_id AND u.action='community_unban' "
                "AND u.target_ap_id=m.target_ap_id AND u.id>m.id) ORDER BY m.id DESC", (community_id,)).fetchall()
        return server_list, rows

    # -- community: posts & comments -------------------------------------------------
    def _object(self, object_id: int) -> Any:
        with self.db.connect() as conn:
            o = conn.execute("SELECT o.*, a.canonical_ap_id AS author_ap_id, a.username AS author_name, "
                             "a.instance AS author_instance FROM objects o LEFT JOIN actors a ON a.id=o.author_id "
                             "WHERE o.id=?", (object_id,)).fetchone()
        if o is None:
            raise AccountError("That post or comment isn't in the archive.")
        return o

    def _object_action(self, account: Account, object_id: int, event: str, reason: str | None,
                       call: Any) -> None:
        obj = self._object(object_id)
        self._require(account, obj["community_id"])

        def act(adapter: Any, token: str) -> None:
            local = self.poster._local_id(adapter, token, account.domain, object_id, obj["object_type"])
            call(adapter, token, obj["object_type"], local)

        self._run(account, act)
        with self.db.transaction() as conn:
            store.add_event(conn, event, utcnow(), object_id=object_id, thread_id=obj["thread_id"],
                            attribution="moderator" if self.powers(account, obj["community_id"]).moderator
                            else "admin", reason=(reason or "").strip() or None,
                            metadata={"via": "threadbnc", "account": account.handle})

    def remove(self, account: Account, object_id: int, reason: str | None = None, removed: bool = True) -> None:
        if removed and not (reason or "").strip():
            raise AccountError("Give a reason for the removal; it's shown in the modlog.")
        reason = (reason or "").strip() or None

        def call(adapter: Any, token: str, kind: str, local: str) -> None:
            (adapter.remove_post if kind == "post" else adapter.remove_comment)(token, local, removed, reason)

        self._object_action(account, object_id, "remove_requested" if removed else "restore_requested",
                            reason, call)

    def lock(self, account: Account, object_id: int, locked: bool = True) -> None:
        def call(adapter: Any, token: str, kind: str, local: str) -> None:
            if kind != "post":
                raise AccountError("Only posts can be locked.")
            adapter.lock_post(token, local, locked)

        self._object_action(account, object_id, "lock_requested" if locked else "unlock_requested", None, call)

    def pin(self, account: Account, object_id: int, pinned: bool = True) -> None:
        def call(adapter: Any, token: str, kind: str, local: str) -> None:
            if kind != "post":
                raise AccountError("Only posts can be pinned.")
            adapter.feature_post(token, local, pinned)

        self._object_action(account, object_id, "pin_requested" if pinned else "unpin_requested", None, call)

    def ban_author(self, account: Account, object_id: int, reason: str, days: int | None = None,
                   remove_content: bool = False) -> NActor:
        obj = self._object(object_id)
        if not obj["author_ap_id"]:
            raise AccountError("The author of that isn't known.")
        return self.community_ban(account, obj["community_id"], obj["author_ap_id"], True, reason, days,
                                  remove_content)

    # -- server (admin) -----------------------------------------------------------------
    def _require_admin(self, account: Account) -> None:
        if not account.is_admin:
            raise AccountError(f"{account.handle} isn't an admin of {account.domain}. "
                               "If that changed, use Refresh on the Accounts page.")

    def site_settings(self, account: Account) -> dict[str, Any]:
        self._require_admin(account)
        settings = self._run(account, lambda adapter, token: adapter.admin_settings(token))
        try:
            settings["banned"] = self._run(account, lambda adapter, token: adapter.site_banned(token))
        except AccountError:
            settings["banned"] = None
        return settings

    def site_ban(self, account: Account, who: str, ban: bool = True, reason: str | None = None,
                 days: int | None = None, remove_content: bool = False) -> NActor:
        self._require_admin(account)
        if ban and not (reason or "").strip():
            raise AccountError("Give a reason for the ban; it's shown in the modlog.")

        def act(adapter: Any, token: str) -> NActor:
            pid, person = adapter.resolve_person(token, who.strip())
            if person.ap_id == account.actor_ap_id:
                raise AccountError("That's you.")
            adapter.site_ban(token, pid, ban, (reason or "").strip() or None, days, remove_content)
            return person

        person = self._run(account, act)
        self._log(account, "site_ban" if ban else "site_unban", target=person, reason=reason, days=days)
        return person

    def _edit_list(self, account: Account, key: str, value: str, add: bool) -> list[str]:
        self._require_admin(account)
        current = self.site_settings(account)
        if not current.get("supports_blocklists"):
            raise AccountError(f"{account.domain}'s software doesn't expose server-wide blocklists through "
                               "its API; use its own admin pages.")
        items = set(current[key])
        if key == "blocked_instances":
            if value == account.domain:
                raise AccountError("You can't block your own server.")
            items = items | {value} if add else items - {value}
        else:
            # Lemmy keeps link blocks as URL prefixes; match on the domain.
            items = {u for u in items if host_of(u if "://" in u else f"https://{u}") != value}
            if add:
                items.add(f"https://{value}/")
        self._run(account, lambda adapter, token: adapter.update_site(token, **{key: sorted(items)}))
        return sorted(items)

    def block_instance(self, account: Account, domain: str, block: bool = True) -> list[str]:
        domain = _clean_domain(domain)
        result = self._edit_list(account, "blocked_instances", domain, block)
        self._log(account, "block_instance" if block else "unblock_instance", target=domain)
        return result

    def block_link_domain(self, account: Account, domain: str, block: bool = True) -> list[str]:
        domain = _clean_domain(domain)
        result = self._edit_list(account, "blocked_urls", domain, block)
        self._log(account, "block_link_domain" if block else "unblock_link_domain", target=domain)
        return result

    def set_registration_mode(self, account: Account, mode: str) -> None:
        self._require_admin(account)
        if mode not in REGISTRATION_MODES:
            raise AccountError("Unknown registration mode.")
        self._run(account, lambda adapter, token: adapter.update_site(token, registration_mode=mode))
        self._log(account, "registration_mode", target=mode)

