"""Acting as Lemmy/PieFed accounts: login, posting, commenting, voting,
editing and deleting your own content.

Every write happens on the account's *home* server (the only place its token
is valid), so targets are resolved there first by ActivityPub id. What the
server returns is recorded in the archive immediately -- your comment shows up
in the thread without waiting for it to federate to the community's server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

from . import store
from .adapters import RemoteAuthError, RemoteError, ThreadiverseAdapter
from .bouncer import Bouncer
from .db import utcnow
from .vault import TokenVault, VaultError

log = logging.getLogger("threadbnc.accounts")
T = TypeVar("T")


class AccountError(Exception):
    """Shown to the user as-is."""


@dataclass
class Account:
    id: int
    domain: str
    username: str
    actor_ap_id: str
    display_name: str | None
    status: str
    is_default: bool
    last_error: str | None

    @property
    def handle(self) -> str:
        return f"{self.username}@{self.domain}"

    @classmethod
    def from_row(cls, r: Any) -> "Account":
        return cls(r["id"], r["domain"], r["username"], r["actor_ap_id"], r["display_name"], r["status"],
                   bool(r["is_default"]), r["last_error"])


def _domain_from(text: str) -> str:
    text = text.strip().lower()
    if "@" in text and "://" not in text:  # user@host
        text = text.rsplit("@", 1)[1]
    host = urlparse(text if "://" in text else f"https://{text}").hostname
    if not host:
        raise AccountError("Enter the account's server, e.g. lemmy.world or dyslectric.dev")
    return host


class Poster:
    def __init__(self, bouncer: Bouncer, vault: TokenVault):
        self.bouncer = bouncer
        self.db = bouncer.db
        self.vault = vault

    # -- account management -------------------------------------------------
    def list(self) -> list[Account]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY is_default DESC, domain, username").fetchall()
        return [Account.from_row(r) for r in rows]

    def get(self, account_id: int | None) -> Account | None:
        if account_id is None:
            return None
        with self.db.connect() as conn:
            r = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return Account.from_row(r) if r else None

    def default(self) -> Account | None:
        accounts = self.list()
        return next((a for a in accounts if a.is_default), accounts[0] if accounts else None)

    def add(self, server: str, username: str, password: str, totp: str | None = None) -> Account:
        """Log in and keep the resulting session token (encrypted). The password
        is used once and never stored. Re-adding an account refreshes its token."""
        domain = _domain_from(server)
        username = username.strip().lstrip("@").split("@")[0]
        if not username or not password:
            raise AccountError("Username and password are required.")
        try:
            adapter = self.bouncer.adapter_for(domain)
            token = adapter.login(username, password, (totp or "").strip() or None)
            me = adapter.whoami(token)
        except RemoteAuthError as exc:
            raise AccountError(f"Login failed: {exc.code or exc}") from exc
        except RemoteError as exc:
            raise AccountError(f"Couldn't reach {domain}: {exc}") from exc
        now = utcnow()
        with self.db.transaction() as conn:
            first = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
            conn.execute(
                "INSERT INTO accounts(domain, software, username, actor_ap_id, display_name, token_enc, status, "
                "is_default, added_at) VALUES (?,?,?,?,?,?,'ok',?,?) "
                "ON CONFLICT(actor_ap_id) DO UPDATE SET token_enc=excluded.token_enc, status='ok', "
                "last_error=NULL, display_name=excluded.display_name, username=excluded.username",
                (domain, adapter.software, me.username, me.ap_id, me.display_name, self.vault.encrypt(token),
                 int(first), now),
            )
            row = conn.execute("SELECT * FROM accounts WHERE actor_ap_id=?", (me.ap_id,)).fetchone()
        return Account.from_row(row)

    def set_default(self, account_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE accounts SET is_default=CASE WHEN id=? THEN 1 ELSE 0 END", (account_id,))

    def remove(self, account_id: int) -> None:
        """Forget an account. Tries to end the session on the server first so
        the token stops working there too."""
        account = self.get(account_id)
        if account is None:
            return
        try:
            adapter, token = self._session(account)
            adapter.logout(token)
        except (RemoteError, AccountError, VaultError) as exc:
            log.info("logout of %s failed (removing anyway): %s", account.handle, exc)
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM my_votes WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            if not conn.execute("SELECT 1 FROM accounts WHERE is_default=1").fetchone():
                conn.execute("UPDATE accounts SET is_default=1 WHERE id=(SELECT MIN(id) FROM accounts)")

    # -- helpers ----------------------------------------------------------------
    def _session(self, account: Account) -> tuple[ThreadiverseAdapter, str]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT token_enc FROM accounts WHERE id=?", (account.id,)).fetchone()
        if row is None or not row["token_enc"] or account.status != "ok":
            raise AccountError(f"{account.handle} needs to log in again (Accounts page).")
        return self.bouncer.adapter_for(account.domain), self.vault.decrypt(row["token_enc"])

    def _run(self, account: Account, fn: Callable[[ThreadiverseAdapter, str], T]) -> T:
        """Run a write as `account`, turning auth failures into a re-login prompt."""
        try:
            adapter, token = self._session(account)
            result = fn(adapter, token)
        except VaultError as exc:
            self._mark(account, "needs_login", str(exc))
            raise AccountError(f"{account.handle}: stored session can't be decrypted; log in again.") from exc
        except RemoteAuthError as exc:
            self._mark(account, "needs_login", str(exc))
            raise AccountError(f"{account.handle} was logged out by its server; log in again.") from exc
        except RemoteError as exc:
            raise AccountError(str(exc)) from exc
        with self.db.transaction() as conn:
            conn.execute("UPDATE accounts SET last_used_at=?, last_error=NULL WHERE id=?", (utcnow(), account.id))
        return result

    def _mark(self, account: Account, status: str, error: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE accounts SET status=?, last_error=? WHERE id=?", (status, error, account.id))

    def _local_id(self, adapter: ThreadiverseAdapter, token: str, domain: str, object_id: int,
                  kind: str) -> str:
        """This object's id on the account's server: cached mapping, else resolve
        its ActivityPub id there (which fetches it over federation if needed)."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT local_id FROM object_local_ids WHERE object_id=? AND domain=?",
                               (object_id, domain)).fetchone()
            obj = conn.execute("SELECT canonical_ap_id FROM objects WHERE id=?", (object_id,)).fetchone()
        if row:
            return row["local_id"]
        if obj is None:
            raise AccountError("That post or comment isn't in the archive.")
        found = adapter.resolve_as(token, obj["canonical_ap_id"])
        if kind not in found:
            raise AccountError(f"{domain} couldn't find that {kind}. It may be deleted, or not reachable "
                               "from that server yet.")
        with self.db.transaction() as conn:
            store.set_local_id(conn, object_id, domain, found[kind])
        return found[kind]

    def _object(self, object_id: int) -> Any:
        with self.db.connect() as conn:
            o = conn.execute("SELECT o.*, a.canonical_ap_id AS author_ap_id FROM objects o "
                             "LEFT JOIN actors a ON a.id=o.author_id WHERE o.id=?", (object_id,)).fetchone()
        if o is None:
            raise AccountError("That post or comment isn't in the archive.")
        return o

    def _require_owner(self, account: Account, obj: Any) -> None:
        if obj["author_ap_id"] != account.actor_ap_id:
            raise AccountError(f"Only the author can do that, and this isn't {account.handle}'s.")

    # -- writing -----------------------------------------------------------------
    def reply(self, account: Account, thread_id: int, body: str, parent_object_id: int | None = None) -> int:
        """Comment on a thread (or reply to a comment in it). Returns the new
        comment's object id in the archive."""
        body = body.strip()
        if not body:
            raise AccountError("Write something first.")
        with self.db.connect() as conn:
            t = conn.execute("SELECT * FROM archived_threads WHERE id=?", (thread_id,)).fetchone()
        if t is None:
            raise AccountError("Thread not found.")
        root_id, parent_id = t["root_object_id"], parent_object_id or t["root_object_id"]

        def act(adapter: ThreadiverseAdapter, token: str) -> Any:
            post_local = self._local_id(adapter, token, account.domain, root_id, "post")
            parent_local = (self._local_id(adapter, token, account.domain, parent_object_id, "comment")
                            if parent_object_id else None)
            return adapter.create_comment(token, post_local, body, parent_local)

        comment = self._run(account, act)
        now = utcnow()
        with self.db.transaction() as conn:
            oid = store.apply_comment(conn, thread_id, root_id, t["community_id"], comment, parent_id,
                                      account.domain, now, False, store.ApplyResult())
        return oid

    def submit(self, account: Account, community_id: int, title: str, body: str | None = None,
               url: str | None = None) -> int:
        """Create a post in a community. It's kept permanently. Returns the thread id."""
        title = title.strip()
        if not title:
            raise AccountError("A post needs a title.")
        with self.db.connect() as conn:
            c = conn.execute("SELECT canonical_ap_id FROM communities WHERE id=?", (community_id,)).fetchone()
        if c is None:
            raise AccountError("Community not found.")

        def act(adapter: ThreadiverseAdapter, token: str) -> Any:
            found = adapter.resolve_as(token, c["canonical_ap_id"])
            if "community" not in found:
                raise AccountError(f"{account.domain} couldn't find that community.")
            return adapter, adapter.create_post(token, found["community"], title, (body or "").strip() or None,
                                                (url or "").strip() or None)

        adapter, post = self._run(account, act)
        return self.bouncer._ingest_post(post, account.domain, post.local_id, adapter, source_url=post.ap_id,
                                         retention="manual")

    def edit(self, account: Account, object_id: int, body: str, title: str | None = None,
             url: str | None = None) -> None:
        obj = self._object(object_id)
        self._require_owner(account, obj)
        kind = obj["object_type"]
        if kind == "post" and not (title or "").strip():
            raise AccountError("A post needs a title.")

        def act(adapter: ThreadiverseAdapter, token: str) -> Any:
            local = self._local_id(adapter, token, account.domain, object_id, kind)
            if kind == "post":
                return adapter.edit_post(token, local, title.strip(), body, (url or "").strip() or None)  # type: ignore[union-attr]
            return adapter.edit_comment(token, local, body)

        result = self._run(account, act)
        now = utcnow()
        with self.db.transaction() as conn:
            if kind == "post":
                store.record_revision(conn, object_id, now, title=result.title, body=result.body, url=result.url,
                                      meta=result.metadata, remote_updated_at=result.updated_at, hidden=False)
            else:
                store.record_revision(conn, object_id, now, title=None, body=result.body, url=None,
                                      meta=result.metadata, remote_updated_at=result.updated_at, hidden=False)

    def delete(self, account: Account, object_id: int, deleted: bool = True) -> None:
        """Delete (or undelete) your own post/comment on its server. The archive
        keeps its text and records the deletion as history."""
        obj = self._object(object_id)
        self._require_owner(account, obj)
        kind = obj["object_type"]

        def act(adapter: ThreadiverseAdapter, token: str) -> Any:
            local = self._local_id(adapter, token, account.domain, object_id, kind)
            if kind == "post":
                return adapter.delete_post(token, local, deleted)
            return adapter.delete_comment(token, local, deleted)

        self._run(account, act)
        # Record the request, not the outcome: the deletion itself is recorded
        # when the bouncer observes it on the community's server, so a lagging
        # server can't produce a spurious "restored" event.
        with self.db.transaction() as conn:
            store.add_event(conn, "delete_requested" if deleted else "undelete_requested", utcnow(),
                            object_id=object_id, thread_id=obj["thread_id"], attribution="author",
                            metadata={"via": "threadbnc", "account": account.handle})

    def vote(self, account: Account, object_id: int, score: int) -> None:
        if score not in (-1, 0, 1):
            raise AccountError("Invalid vote.")
        obj = self._object(object_id)
        kind = obj["object_type"]

        def act(adapter: ThreadiverseAdapter, token: str) -> Any:
            local = self._local_id(adapter, token, account.domain, object_id, kind)
            if kind == "post":
                return adapter.vote_post(token, local, score)
            return adapter.vote_comment(token, local, score)

        result = self._run(account, act)
        now = utcnow()
        with self.db.transaction() as conn:
            if score == 0:
                conn.execute("DELETE FROM my_votes WHERE account_id=? AND object_ap_id=?",
                             (account.id, obj["canonical_ap_id"]))
            else:
                conn.execute("INSERT INTO my_votes(account_id, object_ap_id, score, voted_at) VALUES (?,?,?,?) "
                             "ON CONFLICT(account_id, object_ap_id) DO UPDATE SET score=excluded.score, "
                             "voted_at=excluded.voted_at", (account.id, obj["canonical_ap_id"], score, now))
            # The account's server counts may lag the community's; only take them if present.
            if getattr(result, "upvotes", None) is not None:
                conn.execute("UPDATE objects SET upvotes=?, downvotes=?, score=? WHERE id=?",
                             (result.upvotes, result.downvotes, result.score, object_id))

    def my_votes(self, account: Account | None, ap_ids: list[str]) -> dict[str, int]:
        if account is None or not ap_ids:
            return {}
        marks = ",".join("?" * len(ap_ids))
        with self.db.connect() as conn:
            rows = conn.execute(f"SELECT object_ap_id, score FROM my_votes WHERE account_id=? "
                                f"AND object_ap_id IN ({marks})", [account.id, *ap_ids]).fetchall()
        return {r["object_ap_id"]: r["score"] for r in rows}

    def owned_actor_ids(self) -> set[str]:
        return {a.actor_ap_id for a in self.list()}

