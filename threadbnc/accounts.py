"""Acting as Lemmy/PieFed accounts: login, posting, commenting, voting,
editing and deleting your own content.

Reddit: logging in with Reddit, or connecting with your browser's Reddit
cookie (reddit.py), adds your Reddit account here too.
It isn't in the header switcher: anything on Reddit is done as it
automatically, and everything else as the account picked in the switcher
(see account_for).

Bluesky: signing in with a handle and an app password adds your Bluesky
account (adapters/bluesky.py), which works the same way: anything on Bluesky
is done as it. Its session is refreshed before it runs out, whenever it's used.

Every write happens on the account's *home* server (the only place its token
is valid), so targets are resolved there first by ActivityPub id. What the
server returns is recorded in the archive immediately -- your comment shows up
in the thread without waiting for it to federate to the community's server.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

from . import store
from .adapters import (
    BSKY_DOMAIN, REDDIT_DOMAIN, RemoteAuthError, RemoteError, RemoteRejected, ThreadiverseAdapter, host_of,
    is_bluesky, is_reddit_host, is_rss,
)
from .bouncer import Bouncer
from .db import utcnow
from .vault import TokenVault, VaultError

log = logging.getLogger("threadbnc.accounts")
# Lemmy's default: lowercase letters, digits, underscore; 3 to 20 characters.
_COMMUNITY_NAME = re.compile(r"[a-z0-9_]{3,20}")
TITLE_MAX = 200  # Lemmy's limit; Reddit allows 300
EXCERPT_CHARS = 500  # how much of a feed article a post of it quotes
NO_REDDIT_LOGIN = ("To vote, comment or post on Reddit, log in with Reddit on the Reddit page (an app-only "
                   "connection can only read).")
NO_BLUESKY_LOGIN = "To like, reply or post on Bluesky, log in to Bluesky on the Accounts page."
REDDIT_READ_ONLY_LOGIN = ("Connect Reddit again on the Reddit page to allow voting, commenting and posting; "
                          "this sign-in was made before ThreadBNC asked for that and can only read.")
T = TypeVar("T")
REPOSTED = "#repost"  # my_votes keeps your Bluesky reposts too, as <post's id>#repost


class AccountError(Exception):
    """Shown to the user as-is."""


_FRIENDLY = {
    "only_admins_can_create_communities": "Only admins of that server can create communities.",
    "community_already_exists": "A community with that name already exists on that server.",
    "invalid_name": "That name isn't allowed on that server.",
    "rate_limit_error": "The server is rate-limiting this account; try again in a bit.",
    "site_ban": "This account is banned on its server.",
    "banned_from_community": "This account is banned from that community.",
    "only_mods_can_post_in_community": "Only moderators can post in that community.",
    "locked": "That thread is locked.",
    "too_old": "That Reddit thread is archived: it can't get new comments or votes.",
    "ratelimit": "Reddit is rate-limiting your account; try again in a few minutes.",
    "subreddit_notallowed": "You aren't allowed to post in that subreddit.",
    "subreddit_noexist": "That subreddit doesn't exist.",
}


def _friendly(code: str) -> str | None:
    low = (code or "").lower()
    return next((msg for key, msg in _FRIENDLY.items() if key in low), None)


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
    is_admin: bool = False

    @property
    def is_reddit(self) -> bool:
        return self.domain == REDDIT_DOMAIN

    @property
    def is_bluesky(self) -> bool:
        return self.domain == BSKY_DOMAIN

    @property
    def separate(self) -> bool:
        """Used only for things on its own site (Reddit, Bluesky), never picked in the header."""
        return self.is_reddit or self.is_bluesky

    @property
    def handle(self) -> str:
        if self.is_reddit:
            return f"u/{self.username}"
        return f"@{self.username}" if self.is_bluesky else f"{self.username}@{self.domain}"

    @classmethod
    def from_row(cls, r: Any) -> "Account":
        return cls(r["id"], r["domain"], r["username"], r["actor_ap_id"], r["display_name"], r["status"],
                   bool(r["is_default"]), r["last_error"], bool(r["is_admin"]))


def _excerpt(body: str | None) -> str:
    """The opening paragraphs of an article, up to about EXCERPT_CHARS, without images."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n", body or ""):
        para = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", para).strip()
        if not para or para.startswith("#"):
            continue
        out.append(para)
        if sum(len(p) for p in out) >= EXCERPT_CHARS:
            break
    text = "\n\n".join(out)
    return text if len(text) <= EXCERPT_CHARS * 2 else text[: EXCERPT_CHARS * 2].rsplit(" ", 1)[0] + "…"


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
        # Feeds only shown to someone signed in are read as your Bluesky account.
        bouncer.bluesky_adapter.reading_session = self.bluesky_token

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
        """The account to act as when none is picked. Never the Reddit or
        Bluesky account, which are only ever used for things there."""
        accounts = [a for a in self.list() if not a.separate]
        return next((a for a in accounts if a.is_default), accounts[0] if accounts else None)

    # -- the Reddit account --------------------------------------------------------
    def sync_reddit_account(self) -> None:
        """Make the Accounts page match the Reddit connection: your Reddit
        account while you're logged in with Reddit, nothing otherwise."""
        status = self.bouncer.reddit.status()
        keep = None
        with self.db.transaction() as conn:
            if status and status["has_account"] and status.get("username"):
                keep = f"https://www.reddit.com/user/{status['username']}"
                conn.execute(
                    "INSERT INTO accounts(domain, software, username, actor_ap_id, status, is_default, is_admin, "
                    "added_at, last_error) VALUES (?, 'reddit', ?, ?, ?, 0, 0, ?, ?) "
                    "ON CONFLICT(actor_ap_id) DO UPDATE SET status=excluded.status, last_error=excluded.last_error",
                    (REDDIT_DOMAIN, status["username"], keep, "ok" if status["status"] == "ok" else "needs_login",
                     utcnow(), status.get("last_error")))
            for r in conn.execute("SELECT id FROM accounts WHERE domain=? AND actor_ap_id!=?",
                                  (REDDIT_DOMAIN, keep or "")).fetchall():
                conn.execute("DELETE FROM my_votes WHERE account_id=?", (r["id"],))
                conn.execute("DELETE FROM accounts WHERE id=?", (r["id"],))

    def reddit_account(self) -> Account | None:
        """Your Reddit account, if it can vote, comment and post."""
        status = self.bouncer.reddit.status()
        if not status or not status["can_write"]:
            return None
        return next((a for a in self.list() if a.is_reddit), None)

    def account_for(self, account: Account, ap_id: str) -> Account:
        """Who acts on an object or community: your Reddit account for anything
        on Reddit, otherwise the account you picked. Feeds take no comments,
        votes or posts: share an article with ↗ Post instead."""
        if is_rss(ap_id):
            raise AccountError("Feed articles can't be commented on or voted on, and feeds can't be posted to. "
                               "Use ↗ Post to share the article in one of your communities.")
        if is_bluesky(ap_id):
            bluesky = self.bluesky_account()
            if bluesky is None:
                raise AccountError(NO_BLUESKY_LOGIN)
            return bluesky
        if is_reddit_host(host_of(ap_id)):
            reddit = self.reddit_account()
            if reddit is None:
                status = self.bouncer.reddit.status()
                raise AccountError(REDDIT_READ_ONLY_LOGIN if status and status["mode"] == "user" else NO_REDDIT_LOGIN)
            return reddit
        if account.separate:
            raise AccountError("Add a Lemmy or PieFed account on the Accounts page to do that.")
        return account

    # -- the Bluesky account -------------------------------------------------------
    def bluesky_account(self) -> Account | None:
        return next((a for a in self.list() if a.is_bluesky), None)

    def add_bluesky(self, identifier: str, app_password: str, code: str | None = None) -> Account:
        """Sign in to Bluesky with a handle (or email) and an app password.
        There's one Bluesky account: signing in as another replaces it."""
        if not identifier.strip() or not app_password.strip():
            raise AccountError("Your handle and an app password are both needed.")
        adapter = self.bouncer.adapter_for(BSKY_DOMAIN)
        try:
            token = adapter.login(identifier, app_password.strip(), (code or "").strip() or None)
        except RemoteAuthError as exc:
            if exc.code == "AuthFactorTokenRequired":
                raise AccountError("Bluesky emailed you a sign-in code: enter it too, or use an app password, "
                                   "which doesn't need one.") from exc
            raise AccountError(f"Bluesky didn't accept that: {exc}") from exc
        except RemoteRejected as exc:
            raise AccountError(f"Bluesky refused the sign-in: {exc}") from exc
        except RemoteError as exc:
            raise AccountError(f"Couldn't reach Bluesky: {exc}") from exc
        before = self.bluesky_account()
        account = self.add_session(BSKY_DOMAIN, token)
        if before and before.id != account.id:
            self.remove(before.id)
        return account

    def bluesky_token(self) -> str | None:
        """Your Bluesky session, fresh, for reading feeds only shown to someone
        signed in. None if you aren't, or it no longer works."""
        account = self.bluesky_account()
        if account is None or account.status != "ok":
            return None
        try:
            return self._session(account)[1]
        except (AccountError, RemoteError, VaultError) as exc:
            if isinstance(exc, RemoteAuthError):
                self._mark(account, "needs_login", str(exc))
            log.info("the Bluesky session can't be used: %s", exc)
            return None

    def add(self, server: str, username: str, password: str, totp: str | None = None) -> Account:
        """Log in and keep the resulting session token (encrypted). The password
        is used once and never stored. Re-adding an account refreshes its token."""
        domain = _domain_from(server)
        username = username.strip().lstrip("@").split("@")[0]
        if not username or not password:
            raise AccountError("Username and password are required.")
        try:
            token = self.bouncer.adapter_for(domain).login(username, password, (totp or "").strip() or None)
        except RemoteAuthError as exc:
            raise AccountError(f"Login failed: {exc.code or exc}") from exc
        except RemoteError as exc:
            raise AccountError(f"Couldn't reach {domain}: {exc}") from exc
        return self.add_session(domain, token)

    def add_session(self, domain: str, token: str) -> Account:
        """Keep a session the server gave us (after a password or single
        sign-on login), encrypted, and learn who it belongs to."""
        try:
            adapter = self.bouncer.adapter_for(domain)
            me = adapter.whoami(token)
            roles = adapter.my_roles(token)
        except RemoteAuthError as exc:
            raise AccountError(f"Login failed: {exc.code or exc}") from exc
        except RemoteError as exc:
            raise AccountError(f"Couldn't reach {domain}: {exc}") from exc
        now = utcnow()
        with self.db.transaction() as conn:
            first = domain != BSKY_DOMAIN and conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE domain NOT IN (?,?)", (REDDIT_DOMAIN, BSKY_DOMAIN)).fetchone()[0] == 0
            conn.execute(
                "INSERT INTO accounts(domain, software, username, actor_ap_id, display_name, token_enc, status, "
                "is_default, is_admin, added_at) VALUES (?,?,?,?,?,?,'ok',?,?,?) "
                "ON CONFLICT(actor_ap_id) DO UPDATE SET token_enc=excluded.token_enc, status='ok', "
                "last_error=NULL, display_name=excluded.display_name, username=excluded.username, "
                "is_admin=excluded.is_admin",
                (domain, adapter.software, me.username, me.ap_id, me.display_name, self.vault.encrypt(token),
                 int(first), int(roles.get("admin", False)), now),
            )
            row = conn.execute("SELECT * FROM accounts WHERE actor_ap_id=?", (me.ap_id,)).fetchone()
        return Account.from_row(row)

    def refresh_roles(self, account: Account) -> Account:
        """Re-check whether the account is an admin of its server."""
        roles = self._run(account, lambda adapter, token: adapter.my_roles(token))
        with self.db.transaction() as conn:
            conn.execute("UPDATE accounts SET is_admin=? WHERE id=?", (int(roles.get("admin", False)), account.id))
        return self.get(account.id)  # type: ignore[return-value]

    def create_community(self, account: Account, name: str, title: str, description: str | None = None,
                         nsfw: bool = False, mods_only: bool = False) -> int:
        """Start a community on the account's server (you become its moderator),
        then follow it here with no expiry. Returns the community id."""
        name = name.strip().lower()
        if not _COMMUNITY_NAME.fullmatch(name):
            raise AccountError("Community names are 3-20 lowercase letters, digits or underscores.")
        if not title.strip():
            raise AccountError("A community needs a display title.")
        community = self._run(account, lambda adapter, token: adapter.create_community(
            token, name, title.strip(), (description or "").strip() or None, nsfw, mods_only))
        try:
            cid = self.bouncer.follow_community(community.ap_id, None, None, backfill=True)
        except RemoteError as exc:
            raise AccountError(f"Created !{name}@{account.domain}, but following it failed: {exc}") from exc
        with self.db.transaction() as conn:
            store.add_event(conn, "community_created", utcnow(), community_id=cid,
                            metadata={"via": "threadbnc", "account": account.handle})
        return cid

    def set_default(self, account_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE accounts SET is_default=CASE WHEN id=? THEN 1 ELSE 0 END", (account_id,))

    def remove(self, account_id: int) -> None:
        """Forget an account. Tries to end the session on the server first so
        the token stops working there too."""
        account = self.get(account_id)
        if account is None:
            return
        if account.is_reddit:
            self.bouncer.reddit.disconnect()
            self.sync_reddit_account()
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
                conn.execute("UPDATE accounts SET is_default=1 WHERE id=(SELECT MIN(id) FROM accounts "
                             "WHERE domain NOT IN (?,?))", (REDDIT_DOMAIN, BSKY_DOMAIN))

    # -- helpers ----------------------------------------------------------------
    def _session(self, account: Account) -> tuple[ThreadiverseAdapter, str]:
        if account.is_reddit:  # the Reddit connection holds the sign-in; there's no token here
            status = self.bouncer.reddit.status()
            if not status or status["status"] != "ok":
                raise AccountError("Reddit needs connecting again (Reddit page).")
            return self.bouncer.adapter_for(REDDIT_DOMAIN), ""
        with self.db.connect() as conn:
            row = conn.execute("SELECT token_enc FROM accounts WHERE id=?", (account.id,)).fetchone()
        if row is None or not row["token_enc"] or account.status != "ok":
            raise AccountError(f"{account.handle} needs to log in again (Accounts page).")
        adapter, token = self.bouncer.adapter_for(account.domain), self.vault.decrypt(row["token_enc"])
        if account.is_bluesky:  # its access token lasts a couple of hours: renew it first when it's nearly out
            fresh = adapter.fresh(token)  # type: ignore[attr-defined]
            if fresh != token:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE accounts SET token_enc=? WHERE id=?", (self.vault.encrypt(fresh), account.id))
                token = fresh
        return adapter, token

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
        except RemoteRejected as exc:
            raise AccountError(_friendly(exc.code) or str(exc)) from exc
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
        account = self.account_for(account, self._object(root_id)["canonical_ap_id"])

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
        account = self.account_for(account, c["canonical_ap_id"])
        url = (url or "").strip() or None
        # A Bluesky link card shows the page's title, description and picture: read them first.
        card = {"card": self.bouncer.articles.link_card(url)} if account.is_bluesky and url else {}

        def act(adapter: ThreadiverseAdapter, token: str) -> Any:
            found = adapter.resolve_as(token, c["canonical_ap_id"])
            if "community" not in found:
                raise AccountError(f"{account.domain} couldn't find that community.")
            return adapter, adapter.create_post(token, found["community"], title, (body or "").strip() or None,
                                                url, **card)

        adapter, post = self._run(account, act)
        # A new Bluesky post may not have reached the AppView yet: store it as made, without reading it back.
        return self.bouncer._ingest_post(post, account.domain, post.local_id, adapter, source_url=post.ap_id,
                                         retention="manual", capture=account.is_bluesky)

    # -- reposting -------------------------------------------------------------------
    def repost_draft(self, thread_id: int) -> dict[str, Any]:
        """A new post that shares a thread's post (from Reddit or anywhere else):
        same title and link, the text quoted, and Lemmy's "cross-posted from:"
        line pointing at the original, so readers can find it and ThreadBNC
        shows the two together as duplicates."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT o.canonical_ap_id, r.title, r.body, r.url FROM archived_threads t "
                "JOIN objects o ON o.id=t.root_object_id "
                "JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count WHERE t.id=?", (thread_id,)
            ).fetchone()
        if row is None:
            raise AccountError("Thread not found.")
        title = (row["title"] or "").strip()
        if len(title) > TITLE_MAX:
            title = title[: TITLE_MAX - 1].rstrip() + "…"
        if is_rss(row["canonical_ap_id"]):  # a feed article: its link, and a short excerpt rather than all of it
            excerpt = _excerpt(row["body"])
            quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in excerpt.splitlines())
            return {"title": title, "url": row["url"] or "", "body": quoted, "source": row["url"] or ""}
        body = f"cross-posted from: {row['canonical_ap_id']}"
        text = (row["body"] or "").strip()
        if text and text not in ("[deleted]", "[removed]"):
            body += "\n\n" + "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())
        return {"title": title, "url": row["url"] or "", "body": body, "source": row["canonical_ap_id"]}

    def repost(self, account: Account, thread_id: int, community_id: int, title: str, body: str | None = None,
               url: str | None = None) -> int:
        """Post a copy of a thread's post in one of your communities. Returns
        the new thread's id; the original gets a "reposted" event."""
        tid = self.submit(account, community_id, title, body, url)
        with self.db.transaction() as conn:
            src = conn.execute("SELECT root_object_id FROM archived_threads WHERE id=?", (thread_id,)).fetchone()
            c = conn.execute("SELECT name, canonical_ap_id FROM communities WHERE id=?", (community_id,)).fetchone()
            account = self.account_for(account, c["canonical_ap_id"])
            where = f"r/{c['name']}" if is_reddit_host(host_of(c["canonical_ap_id"])) else \
                f"!{c['name']}@{host_of(c['canonical_ap_id'])}"
            if src:
                store.add_event(conn, "reposted", utcnow(), object_id=src["root_object_id"], thread_id=thread_id,
                                attribution="author",
                                metadata={"via": "threadbnc", "account": account.handle, "thread_id": tid,
                                          "to": where})
        return tid

    def edit(self, account: Account, object_id: int, body: str, title: str | None = None,
             url: str | None = None) -> None:
        obj = self._object(object_id)
        account = self.account_for(account, obj["canonical_ap_id"])
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
        account = self.account_for(account, obj["canonical_ap_id"])
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
        account = self.account_for(account, obj["canonical_ap_id"])
        kind = obj["object_type"]

        def act(adapter: ThreadiverseAdapter, token: str) -> Any:
            local = self._local_id(adapter, token, account.domain, object_id, kind)
            if kind == "post":
                return adapter.vote_post(token, local, score)
            return adapter.vote_comment(token, local, score)

        result = self._run(account, act)
        now = utcnow()
        with self.db.transaction() as conn:
            before = conn.execute("SELECT score FROM my_votes WHERE account_id=? AND object_ap_id=?",
                                  (account.id, obj["canonical_ap_id"])).fetchone()
            old = before["score"] if before else 0
            source = conn.execute("SELECT source_domain FROM archived_threads WHERE id=?",
                                  (obj["thread_id"],)).fetchone()
            if score == 0:
                conn.execute("DELETE FROM my_votes WHERE account_id=? AND object_ap_id=?",
                             (account.id, obj["canonical_ap_id"]))
            else:
                conn.execute("INSERT INTO my_votes(account_id, object_ap_id, score, voted_at) VALUES (?,?,?,?) "
                             "ON CONFLICT(account_id, object_ap_id) DO UPDATE SET score=excluded.score, "
                             "voted_at=excluded.voted_at", (account.id, obj["canonical_ap_id"], score, now))
            # Stored counts are the thread's source server's. Take the answer's counts
            # only from that server: any other server has its own, often far lower,
            # view (e.g. one that only just fetched the post to vote on it). Else
            # count the vote in until the next sync brings the source's counts.
            if source and source["source_domain"] == account.domain and getattr(result, "upvotes", None) is not None:
                conn.execute("UPDATE objects SET upvotes=?, downvotes=?, score=? WHERE id=?",
                             (result.upvotes, result.downvotes, result.score, object_id))
            elif score != old:
                conn.execute("UPDATE objects SET upvotes=upvotes+?, downvotes=downvotes+?, score=score+? WHERE id=?",
                             ((score == 1) - (old == 1), (score == -1) - (old == -1), score - old, object_id))

    # -- Bluesky reposts and quotes ------------------------------------------------------
    def _bluesky_target(self, object_id: int) -> tuple[Account, Any]:
        obj = self._object(object_id)
        if not is_bluesky(obj["canonical_ap_id"]):
            raise AccountError("Only Bluesky posts can be reposted or quoted there.")
        return self.account_for(self.bluesky_account() or self.default(), obj["canonical_ap_id"]), obj  # type: ignore[arg-type]

    def bluesky_repost(self, object_id: int, on: bool = True) -> None:
        """Repost a Bluesky post or reply to your followers there, or undo it."""
        account, obj = self._bluesky_target(object_id)
        kind = obj["object_type"]
        self._run(account, lambda adapter, token: adapter.repost(  # type: ignore[attr-defined]
            token, self._local_id(adapter, token, account.domain, object_id, kind), on))
        key = obj["canonical_ap_id"] + REPOSTED
        with self.db.transaction() as conn:
            if on:
                conn.execute("INSERT INTO my_votes(account_id, object_ap_id, score, voted_at) VALUES (?,?,1,?) "
                             "ON CONFLICT(account_id, object_ap_id) DO UPDATE SET voted_at=excluded.voted_at",
                             (account.id, key, utcnow()))
            else:
                conn.execute("DELETE FROM my_votes WHERE account_id=? AND object_ap_id=?", (account.id, key))

    def bluesky_quote(self, object_id: int, text: str) -> int:
        """Post on your Bluesky account quoting a post or reply. Returns the new post's thread id."""
        if not text.strip():
            raise AccountError("Write something first.")
        account, obj = self._bluesky_target(object_id)
        kind = obj["object_type"]
        adapter, post = self._run(account, lambda adapter, token: (adapter, adapter.quote(  # type: ignore[attr-defined]
            token, self._local_id(adapter, token, account.domain, object_id, kind), text)))
        return self.bouncer._ingest_post(post, account.domain, post.local_id, adapter, source_url=post.ap_id,
                                         retention="manual", capture=True)

    def my_reposts(self, account: Account | None, ap_ids: list[str]) -> set[str]:
        """Which of these Bluesky posts you've reposted from here."""
        return {k[: -len(REPOSTED)] for k in self.my_votes(account, [a + REPOSTED for a in ap_ids])}

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

