"""Your accounts' inboxes: replies to your posts and comments, mentions, and
private messages.

The bouncer checks each account's server every few minutes (Reddit less
often) and keeps what it finds in `inbox_items`. Read state is the server's:
marking something read here marks it read there, and something read in
another app shows as read here after the next check.

Replying happens on the account's own server, where the ids stored with each
item are valid, so a reply works whether or not the thread is in the archive.
When it is, the reply is recorded there straight away, like any other comment
made through ThreadBNC.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from . import store
from .accounts import Account, AccountError, Poster
from .adapters import NInboxItem
from .config import REDDIT_MIN_POLL_MINUTES
from .db import parse_ts, utcnow

log = logging.getLogger("threadbnc.inbox")

KINDS = {"reply": "Replies", "mention": "Mentions", "message": "Messages"}
PAGE_SIZE = 50
NO_REDDIT_INBOX = ("Connect Reddit again on the Reddit page to see your Reddit inbox; this sign-in was made "
                   "before ThreadBNC asked for it.")


def attach(bouncer: Any, settings: Any) -> "Inbox":
    """Inbox checks for a bouncer running on its own (``python -m threadbnc bouncer``)."""
    from .vault import TokenVault

    inbox = Inbox(Poster(bouncer, TokenVault(settings.credentials_key, settings.data_dir)),
                  settings.inbox_poll_minutes)
    bouncer.hooks.append(inbox.sweep)
    return inbox


class Inbox:
    def __init__(self, poster: Poster, every_minutes: int = 5):
        self.poster = poster
        self.db = poster.db
        self.every = timedelta(minutes=max(1, every_minutes))
        self.reddit_every = timedelta(minutes=max(REDDIT_MIN_POLL_MINUTES, every_minutes))

    # -- checking ------------------------------------------------------------------
    def can_check(self, account: Account) -> str | None:
        """Why this account's inbox can't be checked, or None."""
        if account.status != "ok":
            return f"{account.handle} needs to log in again (Accounts page)."
        if account.is_bluesky:
            return "Bluesky notifications aren't read here yet."
        if account.is_reddit:
            status = self.poster.bouncer.reddit.status()
            if not status or not status.get("can_inbox"):
                return NO_REDDIT_INBOX
        return None

    def sweep(self, force: bool = False) -> int:
        """Check every account whose turn it is. Called from the bouncer's
        loop. Returns how many new items arrived."""
        now = parse_ts(utcnow())
        new = 0
        with self.db.connect() as conn:
            checked = {r["id"]: r["inbox_checked_at"] for r in conn.execute("SELECT id, inbox_checked_at FROM accounts")}
        for account in self.poster.list():
            last = parse_ts(checked.get(account.id))
            every = self.reddit_every if account.is_reddit else self.every
            if not force and last and now - last < every:  # type: ignore[operator]
                continue
            if self.can_check(account):
                continue
            try:
                new += self.check(account)
            except AccountError:
                pass  # noted on the account; tried again at its next turn
        return new

    def check(self, account: Account) -> int:
        """Fetch the account's newest inbox items. Returns how many are new."""
        problem = self.can_check(account)
        if problem:
            self._note(account, problem)
            raise AccountError(problem)
        try:
            items = self.poster._run(account, lambda adapter, token: adapter.inbox(token, account.actor_ap_id))
        except AccountError as exc:
            self._note(account, str(exc))
            log.info("inbox of %s: %s", account.handle, exc)
            raise
        now = utcnow()
        with self.db.transaction() as conn:
            known = {(r["kind"], r["remote_id"]) for r in conn.execute(
                "SELECT kind, remote_id FROM inbox_items WHERE account_id=?", (account.id,))}
            new = 0
            for item in items:
                new += (item.kind, item.remote_id) not in known
                self._store(conn, account, item, now)
            conn.execute("UPDATE accounts SET inbox_checked_at=?, inbox_error=NULL WHERE id=?", (now, account.id))
        return new

    def _store(self, conn: Any, account: Account, item: NInboxItem, now: str) -> None:
        c = item.community
        who = item.author.username
        if account.is_reddit:
            who = who if who.startswith("r/") else f"u/{who}"  # messages from a subreddit's moderators
        else:
            who = f"{who}@{item.author.domain}"
        conn.execute(
            """INSERT INTO inbox_items(account_id, kind, remote_id, unread, author_ap_id, author_name,
                   author_local_id, body, subject, created_at, deleted, object_type, object_ap_id, object_local_id,
                   post_ap_id, post_local_id, post_title, community_ap_id, community_name, first_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id, kind, remote_id) DO UPDATE SET unread=excluded.unread, body=excluded.body,
                   deleted=excluded.deleted, post_title=excluded.post_title, updated_at=excluded.updated_at""",
            (account.id, item.kind, item.remote_id, int(item.unread), item.author.ap_id, who, item.author_local_id,
             item.body, item.subject, item.created_at, int(item.deleted), item.object_type, item.object_ap_id, item.object_local_id, item.post_ap_id, item.post_local_id, item.post_title,
             c.ap_id if c else None, c.name if c else None, now, now))

    def _note(self, account: Account, error: str) -> None:
        # Counts as a check, so a failing server is retried at the usual pace rather than every pass.
        with self.db.transaction() as conn:
            conn.execute("UPDATE accounts SET inbox_checked_at=?, inbox_error=? WHERE id=?",
                         (utcnow(), error, account.id))

    # -- reading -----------------------------------------------------------------------
    def unread_count(self) -> int:
        with self.db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM inbox_items WHERE unread=1").fetchone()[0]

    def items(self, *, unread_only: bool = False, kind: str | None = None, account_id: int | None = None,
              page: int = 1) -> tuple[list[dict[str, Any]], bool]:
        """Newest first, with where each comment lives in the archive (if it's
        there). Returns (items, whether there are more)."""
        where, args = ["1=1"], []
        if unread_only:
            where.append("i.unread=1")
        if kind in KINDS:
            where.append("i.kind=?")
            args.append(kind)
        if account_id:
            where.append("i.account_id=?")
            args.append(account_id)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT i.*, o.id AS archived_oid, o.thread_id AS archived_tid, p.thread_id AS post_tid
                    FROM inbox_items i
                    LEFT JOIN objects o ON o.canonical_ap_id=i.object_ap_id
                    LEFT JOIN objects p ON p.canonical_ap_id=i.post_ap_id AND p.object_type='post'
                    WHERE {' AND '.join(where)}
                    ORDER BY COALESCE(i.created_at, i.first_seen_at) DESC, i.id DESC LIMIT ? OFFSET ?""",
                [*args, PAGE_SIZE + 1, (max(1, page) - 1) * PAGE_SIZE]).fetchall()
        accounts = {a.id: a for a in self.poster.list()}
        out = []
        for r in rows[:PAGE_SIZE]:
            d = dict(r)
            d["account"] = accounts.get(r["account_id"])
            d["thread_id"] = r["archived_tid"] or r["post_tid"]
            out.append(d)
        return out, len(rows) > PAGE_SIZE

    def status(self) -> list[dict[str, Any]]:
        """Each account: when its inbox was last checked, and any problem."""
        with self.db.connect() as conn:
            rows = {r["id"]: r for r in conn.execute(
                "SELECT a.id, a.inbox_checked_at, a.inbox_error, "
                "(SELECT COUNT(*) FROM inbox_items i WHERE i.account_id=a.id AND i.unread=1) AS unread "
                "FROM accounts a")}
        out = []
        for account in self.poster.list():
            if account.is_bluesky:  # no inbox read for it (can_check)
                continue
            r = rows.get(account.id)
            out.append({"account": account, "checked_at": r["inbox_checked_at"] if r else None,
                        "error": self.can_check(account) or (r["inbox_error"] if r else None),
                        "unread": r["unread"] if r else 0})
        return out

    # -- acting on items -----------------------------------------------------------------
    def _item(self, item_id: int) -> tuple[Any, Account]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM inbox_items WHERE id=?", (item_id,)).fetchone()
        account = self.poster.get(row["account_id"]) if row else None
        if row is None or account is None:
            raise AccountError("That inbox item is gone.")
        return row, account

    def mark_read(self, item_id: int, read: bool = True) -> None:
        row, account = self._item(item_id)
        self.poster._run(account, lambda adapter, token: adapter.mark_inbox_read(token, row["kind"], row["remote_id"],
                                                                                  read))
        with self.db.transaction() as conn:
            conn.execute("UPDATE inbox_items SET unread=?, updated_at=? WHERE id=?", (int(not read), utcnow(), item_id))

    def mark_all_read(self, account_id: int | None = None) -> int:
        """Mark everything read, on each account's server. Returns how many
        items that was; accounts that fail are skipped and reported."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id, account_id, kind, remote_id FROM inbox_items WHERE unread=1"
                                + (" AND account_id=?" if account_id else ""),
                                (account_id,) if account_id else ()).fetchall()
        by_account: dict[int, list[Any]] = {}
        for r in rows:
            by_account.setdefault(r["account_id"], []).append(r)
        done, errors = 0, []
        for aid, group in by_account.items():
            account = self.poster.get(aid)
            if account is None:
                continue
            pairs = [(r["kind"], r["remote_id"]) for r in group]
            try:
                self.poster._run(account, lambda adapter, token: adapter.mark_all_inbox_read(token, pairs))
            except AccountError as exc:
                errors.append(f"{account.handle}: {exc}")
                continue
            with self.db.transaction() as conn:
                conn.execute("UPDATE inbox_items SET unread=0, updated_at=? WHERE account_id=? AND unread=1",
                             (utcnow(), aid))
            done += len(group)
        if errors and not done:
            raise AccountError("; ".join(errors))
        return done

    def reply(self, item_id: int, body: str) -> int | None:
        """Answer an item as the account it came to: a reply under the comment
        or post, or a private message back. Marks the item read. Returns the
        reply's object id when the thread is in the archive."""
        body = body.strip()
        if not body:
            raise AccountError("Write something first.")
        row, account = self._item(item_id)
        if row["kind"] == "message":
            if not row["author_local_id"] and not account.is_reddit:
                raise AccountError("Can't tell who sent that message, so it can't be answered from here.")
            self.poster._run(account, lambda adapter, token: adapter.send_message(
                token, row["author_local_id"], body, in_reply_to=row["remote_id"]))
            oid = None
        else:
            if not row["post_local_id"]:
                raise AccountError("Can't tell which post that was on, so it can't be answered from here.")
            if account.is_reddit and self.poster.reddit_account() is None:
                raise AccountError("To reply on Reddit, log in with Reddit on the Reddit page (this sign-in can "
                                   "only read).")
            parent = row["object_local_id"] if row["object_type"] == "comment" else None
            comment = self.poster._run(account, lambda adapter, token: adapter.create_comment(
                token, row["post_local_id"], body, parent))
            oid = self._archive_reply(row, account, comment)
        try:
            if row["unread"]:
                self.mark_read(item_id)
        except AccountError as exc:  # the reply went through; being left unread isn't worth failing over
            log.info("marking inbox item %s read failed: %s", item_id, exc)
        return oid

    def _archive_reply(self, row: Any, account: Account, comment: Any) -> int | None:
        """Record the new comment in the archive, if its thread is there."""
        with self.db.transaction() as conn:
            t = conn.execute("SELECT t.id, t.root_object_id, t.community_id FROM objects p "
                             "JOIN archived_threads t ON t.id=p.thread_id WHERE p.canonical_ap_id=?",
                             (row["post_ap_id"],)).fetchone()
            if t is None or comment is None:
                return None
            parent = conn.execute("SELECT id FROM objects WHERE canonical_ap_id=?",
                                  (row["object_ap_id"],)).fetchone() if row["object_type"] == "comment" else None
            return store.apply_comment(conn, t["id"], t["root_object_id"], t["community_id"], comment,
                                       parent["id"] if parent else t["root_object_id"], account.domain, utcnow(),
                                       False, store.ApplyResult())
