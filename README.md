# ThreadBNC — Threadiverse bouncer + private archive

A private, feed-first reader for Lemmy, PieFed, Reddit and RSS/Atom feeds that keeps a history of what it observes. When a post or comment is edited, removed or deleted after the bouncer has seen it, the change is recorded alongside the earlier version instead of replacing it.

**Following and reading:**
- Follow communities. Lemmy and PieFed posts arrive as they're made, pushed through your own Lemmy server (see [Pushes from your own server](#pushes-from-your-own-server)); feeds and subreddits are checked on a schedule.
- Your **feed** is built from the saved copy. Posts you haven't opened stand out, opened posts show "N new comments", and you can sort by New, Active, Top or Most comments.
- **Opening a post** reads its comments and saves the article it links to, like a browser would. The page shows the saved copy at once and swaps in the fresh one.
- The feed keeps working when an instance is down, and shows edits, removals and deletions as history instead of losing them.

**Keeping:**
- **☆ Keep** a post to hold on to it with no expiry date, until you unkeep or delete it. Posts you haven't kept expire after the community's retention period.
- You can also keep a single post by pasting its link on the **Kept** page, without following its community.

### What it does and doesn't capture

Stored observations aren't overwritten: edits add a new version, and removals and deletions become history entries. The archive can only keep what the bouncer actually saw, though, and it deletes some things on purpose.

**What it can miss**
- **Comments in posts you don't open,** unless they're pushed. In a community pushed through your own server, every comment, edit, deletion and removal arrives as it happens. Everywhere else (subreddits, communities checked on a schedule, posts kept by link) comments are only read when you open the post, so:
  - A comment posted and deleted between two of your visits is never seen.
  - Several edits between visits show up as one change.
- **Communities that aren't arriving.** A Lemmy or PieFed community your server can't subscribe to brings nothing new until you turn on checking it on a schedule (its **Following** menu says which).
- **Subreddits while you're away.** They're only checked while you're using ThreadBNC, so posts made and deleted while you're away aren't seen, and a very busy subreddit can have gaps after a long absence.
- **Content that was already gone.** If a comment was deleted or removed before the bouncer first saw it, only its placeholder is stored.
- **Some media.** Images or videos over the size limit (25 MB by default, or set per community) and downloads that keep failing are not saved. A link to the original is kept instead. Communities that have transcoding turned on keep a smaller copy of oversized files instead (see [Media and Markdown](#media-and-markdown)).
- **Some linked articles.** Paywalled pages, sites that refuse the bouncer, and pages with too little text aren't saved, and the article is kept as the bouncer first read it, not as later updated (see [Linked articles](#linked-articles)).
- **Outages.** Nothing is lost while an instance is unreachable, but changes that happen and are reversed during the outage won't be seen.

**What it deletes, by design**
- Posts from followed communities that you haven't kept, once their retention period ends.
- Threads in the trash, once the trash period ends, or straight away if you choose **Delete now**.
- Images that nothing else references, when those threads are deleted.

Everything else stays, but the archive is only as durable as the disk and database it runs on. Back up both; see [Backups](#deploy-on-a-server-docker-compose--postgres).

### What it asks of other servers

ThreadBNC behaves like one more subscribed server, or like your own browser, never like a crawler:

- **Pushed, not polled.** Lemmy and PieFed communities are subscribed to by your own server, and their home servers send each post, comment and edit once, as federation intends. Nothing is checked on a schedule unless you turn it on for a community that can't be pushed, and then it's one listing per check, never comments.
- **Fetched when you open it.** A post's comments, its linked article and its full videos are fetched when you open or keep it, and reopening within 5 minutes uses the saved copy.
- **Votes, cheaply.** Votes are updated every 5 minutes for a post's first half hour, then every 10, every 30 until 6 hours, hourly until a day, daily until a week, and then not at all (opening a post still updates them). A pushed community's come from your own server; a checked community's from one listing covering all its posts; only a post kept on its own is asked about by itself.
- **Reddit only while you're here,** spread out, one subreddit at a time (see [Checking, conservatively](#checking-conservatively)).
- **Pictures at once, videos later.** Pictures and thumbnails are downloaded as posts arrive; full videos wait until you open or keep a post showing them.
- **One request at a time,** at least a second apart per server (two for Reddit). A server that answers 429 Too Many Requests isn't contacted again until its `Retry-After` has passed, and that isn't counted as a failure.
- Feeds are checked on a schedule, as feeds are meant to be, with conditional requests.

## Pages

| Page | What it's for |
|---|---|
| **Feed** (`/`) | Posts from every followed community. Sort by New, Active, Top or Most comments; filter by day, week, month or all time; show only unread posts, read posts with new comments, or either; mark posts read one at a time or all at once. **Make this the default** in the feed's ⚙ menu keeps the current sort, time range and filter as what the feed (and community pages) show when you open them. The sidebar lists your own feeds and the communities you follow, with unread counts; the Following list collapses. |
| **Your own feeds** (`/f/{id}`, **New feed** in the sidebar) | A named mix of communities, each feed with its own default sort, time range, filter and view. Mark all read covers just its communities. The ✎ next to its name edits or deletes it; the communities and their posts are untouched. |
| **Community** (`/c/{id}`) | The same feed for one community, plus ★ Kept, **Live on server** (browse its full history, fetched live), **Media** (what it archives, size limit, transcoding) and a log. Follow settings sit behind the "✓ Following" pill. |
| **Communities** | Follow a community (starts with its current first page) and manage the check interval and retention for each one. |
| **★ Kept** | Keep a post by link, see kept threads grouped by community, recent changes and the bouncer queue. |
| **Search** (`/search`, and the box in the header) | Every version of every archived post and comment. See [Search](#search). |
| **Inbox** | Replies, mentions and private messages for all your accounts. See [Inbox](#inbox). |
| **Storage** | How much space the archive takes: media and text by kind of content, by kind of thread (kept, auto-captured, in the trash) and by community, plus the database size and free disk space. It also links to archive integrity checks and a portable export. Each community links to its Media settings. |
| **Trash** | Hidden and unkept threads, restorable until the trash period ends. |
| **Reddit** (`/reddit`, linked from Accounts) | Connect Reddit, and follow your Reddit subscriptions. |

**Posts, pictures or grid:** feeds can show posts three ways.
- **Posts** shows a thumbnail, the title and the start of the text.
- **Pictures** shows one post per row, with its pictures at full width.
- **Grid** shows tiles: the image or video cropped square, with the title, community, comments, votes, and small ☆ Keep, ✕ Hide and ↻ Repost buttons. Posts without media become text tiles.
- Until you choose, each community picks for itself: the grid when at least 60% of its 40 most recent posts have an archived image or video (and it has at least 4 posts), posts otherwise. The home feed goes by what's on the page.
- The three view buttons in the feed controls override that. The choice is remembered per community, and separately for the home feed. **auto** in the options menu goes back to deciding by itself.
- **Several pictures:** posts with more than one picture (a Reddit gallery, or several images in the text) show a counter such as 1/4 in the Pictures and Grid views. Swipe, use the ‹ › buttons, or press **h** / **l** (or ← / →) on the selected post to go through them. In the Posts view the thumbnail shows how many there are.
- NSFW and spoiler images are blurred until you hover over or focus the tile.

**Keep and Hide:**
- **☆ Keep** holds a post with no expiry date.
- **★ Kept** unkeeps it. A post that came from a followed community goes back into the feed and expires normally. A post you kept by link goes to the trash.
- **Hide** moves a feed post to the trash.

## Search

Search covers every version of every post and comment the bouncer saved, so text that was later edited away,
deleted or removed can still be found. Results show what the post or comment says now. When only an earlier
version matched, the result says so, quotes that version, and links to the item's history.

| Type | To find |
|---|---|
| `restic borg` | posts and comments with both words |
| `"self hosted"` | that exact phrase |
| `docker*` | words starting with docker |
| `caddy OR nginx` | either word |
| `backup -windows` | backup, leaving out anything that mentions windows |

- Words are matched whole, ignoring case and accents. There's no stemming, since the archive holds many languages, so `backup` doesn't match `backups`; use `backup*`.
- Titles rank above text, and text above links.
- **Filters:** posts or comments, one community, one author (`name` or `name@server`), kept only, and only items that were deleted or removed, or edited. Sort by best match, newest or oldest.
- Each community page has its own search box, limited to that community.
- Threads in the trash aren't searched. Purged threads are dropped from the index along with the rest of their data.
- The index is SQLite's FTS5 or a Postgres GIN index. It's built on first start after upgrading, which can take a while on a big archive, and kept up to date from then on. If your SQLite was built without FTS5, search still works, but more slowly, and it matches parts of words.

## Duplicates and crossposts

When the same link or the same text post shows up more than once, whether reposted by different people or crossposted to several communities, ThreadBNC shows it once.

**What counts as the same post**
- **Links** match after normalising: `http`/`https`, `www.` and `m.`, trailing slashes, `#fragments` and tracking parameters (`utm_*`, `fbclid`, `si`, …) are ignored, and `youtu.be/ID`, `youtube.com/shorts/ID` and `youtube.com/watch?v=ID` are one video.
- **Text posts** match on title and body, ignoring case, spacing, quoting and Lemmy's `cross-posted from:` line. A post with a title and no body never counts as a duplicate, so recurring "Weekly thread" posts aren't merged.

**In the feed** the copies make one card. It lists every community and poster, and adds up the comments and votes. Hover over (or focus) the votes to see what each server reported. **Keep** and **Hide** act on every copy.

**On the thread page** the post lists each copy with its own title, votes and comment count. Comments from every copy form one tree:
- Each comment is tagged with the community it was posted under.
- If the same person posted the same text more than once (for example under each crosspost, or by double-submitting), it's squashed into one comment marked **×N**. Its votes are added up, with a per-server breakdown on hover, and replies to every copy appear under it.
- **Comment** has a checkbox for each copy of the post. Every copy is ticked except locked, removed or deleted ones. One comment is made under each ticked copy.
- **Reply** to a squashed comment works the same way, with a checkbox for each copy of that comment.
- **Votes** on a combined post or squashed comment go to every copy.
- **Mod** tools, **Edit** and **Delete** act on the first copy only; the Mod panel names which one.
- Opening the thread marks every copy as read. **Show only this one** (`?merge=0`) shows a single copy on its own.

## Reddit

Subreddits work like any other community: follow `r/name` (or paste `https://www.reddit.com/r/name`) and its
posts land in your feed. When a post scrolls into view in the feed, its text, pictures and votes are read, in one
request for every post on screen, and not again for five minutes. Opening one reads its comments too, and edits,
removals and deletions seen then are kept as history. You can also keep a single Reddit post by pasting its link on the **Kept** page; share links from
the Reddit app (`/r/name/s/…`) work too.

Logged in with Reddit, you can also vote, comment, reply, post, edit and delete there. To discuss a Reddit post on
your own server instead, **repost** it.

### Connecting

The simplest way is **your browser's cookie**, which needs no Reddit app:

1. In a browser that's logged in to Reddit, open reddit.com and press F12.
2. Chrome or Edge: *Application* → *Cookies* → `https://www.reddit.com`. Firefox: *Storage* → *Cookies*.
3. Copy the value of `reddit_session` and paste it on the **Reddit** page.

- ThreadBNC then reads through reddit.com's own `.json` pages as you, and votes, comments and posts through the endpoints old.reddit uses. Your subscriptions, the auto-used Reddit account and reposting all work the same.
- The cookie is as powerful as your password until that session ends, so it's stored encrypted (`THREADBNC_CREDENTIALS_KEY`) and never shown again. Logging out of Reddit in that browser ends it. ThreadBNC then stops and asks for a fresh one, instead of retrying.
- Reddit's terms don't provide for this kind of access, so there's some risk to the account, more so when voting and posting.
- ThreadBNC keeps to the same polling limits. It identifies itself in its User-Agent rather than posing as a browser. If Reddit refuses such requests, ThreadBNC reports it.

Alternatively, use a Reddit app. Since November 2025 Reddit only issues new apps after approval, so this is mainly for
apps you created earlier, which keep working. Connect one on the **Reddit** page in one of two ways:

| | App type | What you enter | What you get |
|---|---|---|---|
| **Log in with Reddit** | web app (or installed app, which has no secret) | client id and secret; you then approve ThreadBNC on reddit.com | Reads as your account, lists your subscriptions so you can follow them in one go, and votes, comments and posts as you |
| **App only** (API key) | web app or script | client id and secret | Reads public subreddits, no Reddit account involved; can't vote, comment or post |

- For **Log in with Reddit**, set the app's redirect uri to the address the Reddit page shows, `https://<your ThreadBNC>/reddit/callback`.
- Your Reddit password never passes through ThreadBNC. It keeps the refresh token Reddit returns and the app secret, both encrypted with `THREADBNC_CREDENTIALS_KEY`, like account sessions.
- **Disconnect** ends the sign-in on Reddit too. Followed subreddits and everything saved from them stay, but aren't checked until you connect again.
- Reddit may require you to request API access before a new app works.
- Reddit logins made before ThreadBNC could vote and comment can only read. The Reddit page marks them **read only**; log in again to allow writing.
- Logins made before the [inbox](#inbox) existed can't read your Reddit inbox. Log in again to allow it. Cookie connections can already read it.

### Voting, commenting and posting on Reddit

Logging in with Reddit adds your Reddit account (`u/name`) to the Accounts page. It isn't in the header's account
switcher. **Anything on Reddit is done as your Reddit account automatically**, and everything else as the account
picked in the switcher, so you never switch just to upvote a Reddit comment.

| Where | What you can do |
|---|---|
| Reddit thread page | Vote on the post and on each comment, comment, reply |
| Your own Reddit posts and comments | Edit and delete |
| Subreddit page | **✎ New post**, as a link or text post |
| **↻ Repost** | Post a copy to a subreddit you follow, as well as to your own communities |

- On a group that mixes Reddit and Lemmy/PieFed copies, each copy gets its own account: one vote or comment goes to every copy, as your Reddit account on Reddit and as the picked account elsewhere.
- What Reddit sends back is archived straight away, as with Lemmy. When you next open the thread it recognises your comment rather than adding it again.
- Reddit doesn't allow some things, and ThreadBNC says so instead of trying: changing a post's title or link (only its text), or restoring a deleted post or comment. Archived threads (usually older than six months) take no new comments or votes.
- Reddit is stricter about writing through the API than about reading. A new API client that posts or votes a lot can be flagged as spam, so use this at a human pace.
- Moderating subreddits from ThreadBNC isn't supported.

### Checking, conservatively

One app gets about 100 requests a minute from Reddit, shared by everything ThreadBNC does, and Reddit blocks
clients that go over. So Reddit is asked about the way you'd browse it:

- **Only while you're using ThreadBNC.** A tab that's visible and was used in the last half hour counts; the page tells the server at most once a minute. Away longer, nothing is asked of Reddit.
- **Subreddits** are checked every 60 minutes by default (`THREADBNC_REDDIT_POLL_MINUTES`), and never more often than every 10, whatever you set. They're checked one at a time, spread across that interval, so coming back doesn't set off a burst. Each check reads at most 2 pages of 25 new posts (the first check reads one), and updates the votes on posts already here.
- **Posts are stored as title, link and pictures.** Their text and comments are read when you open one, and again when you reopen it more than 5 minutes later. Filling in the text isn't recorded as an edit.
- **Big threads**: each fetch reads the newest 200 comments. Reddit hides the rest behind "load more comments", which ThreadBNC doesn't expand. A comment that drops out of view isn't marked as gone; only a complete comment tree can show that.
- **Spacing**: at most one request every 2 seconds (`THREADBNC_REDDIT_MIN_REQUEST_INTERVAL`). When Reddit's rate-limit headers say the allowance is nearly used, or it answers 429, ThreadBNC waits for the window to reset, or as long as Reddit's `Retry-After` asks if that's longer.
- **Sign-in problems** stop everything until you reconnect, so a revoked or expired sign-in isn't retried against Reddit.
- **Outages** are handled as they are for Lemmy: a failure, a 403 (private or quarantined subreddit) or a pause is retried with exponential backoff and never counts as a deletion.

### Reposting

**↻ Repost** appears on every thread page, and on Reddit posts in the feed. It works from Reddit to your server, and,
when you're logged in with Reddit, from your server to a subreddit you follow. It opens a new post that is already filled in:

- the same title (trimmed to Lemmy's 200 characters) and link;
- a `cross-posted from: <original>` line, and the original text quoted underneath.

Then pick where it goes:

- By default the list shows communities on the server of the account you're posting as, then ones you follow, then subreddits you follow. The community you used last is selected next time.
- You can type any other `!name@server` (or `r/name`) instead.
- You can edit anything before posting.

The new post is kept automatically. It has the same link, or the same text once the crosspost line and quoting are
ignored, so ThreadBNC shows it together with the Reddit original as [duplicates](#duplicates-and-crossposts).
The original records **You reposted this to !community@server**.

When you aren't logged in with Reddit, votes and comments on a mixed group go only to the non-Reddit copies.

## RSS and Atom feeds

Feeds are followed like communities: paste the feed's address, or just a site's address if the site advertises a
feed, into the follow box. RSS 2.0, RSS 1.0 (RDF) and Atom all work. Articles land in your feed like posts, with
edits kept as history, keeping and expiry, duplicate grouping and tiles.

- **Article text** is converted from HTML to Markdown: paragraphs, headings, emphasis, links, lists, quotes and code. Scripts and styles are dropped. Images in articles, and a feed's thumbnails, are archived like any other post's media, so photo feeds look good as tiles.
- **↗ Post** (on feed articles in the feed, and on the article's page) shares one in one of your communities: a link post with the article's title and link, and a short quoted excerpt you can edit or remove. The copy you post is kept, and shown together with the article, because they share the link.
- **Feeds have no comments or votes**, so article pages have no comment box. Once you post an article, its comments are on your copy, shown on the same page.
- **Checking** is every 60 minutes by default (`THREADBNC_RSS_POLL_MINUTES`), and never more often than every 5. It uses conditional requests, so an unchanged feed costs one short "not modified" answer. One fetch serves every article from that feed for 5 minutes. Articles aren't re-checked for edits once saved.
- **Feeds only list their latest entries.** An article that drops out of its feed is kept as last seen and marked "Dropped out of its feed", not recorded as missing, and isn't checked any more.
- Fetches follow at most 5 redirects, refuse private and local addresses, and stop at 5 MB.

### OPML import and export

The **Communities** page imports and exports OPML 2.0 subscription lists. Import accepts nested folders, ignores
duplicate URLs, and follows each HTTP(S) RSS or Atom feed with the chosen check interval and retention; feeds
you already follow keep their own settings. Export
contains every active RSS/Atom follow whose canonical feed address is an HTTP(S) URL.

Lemmy, PieFed and Reddit communities are deliberately not written to OPML: they are not RSS subscriptions.
ThreadBNC's synthetic YouTube sources are also omitted rather than exporting an address that another reader—or a
later ThreadBNC import—could not reliably follow as the same source.

## YouTube

Paste a channel or playlist link into the follow box: `youtube.com/@name`, `/channel/UC…`, `/user/…` or
`/playlist?list=…`. It gets its own **YouTube** group in the sidebar and in the feed editor. An `@name` or `/c/`
link is read once, when you follow it, to find the channel's id.

- **Checking:** YouTube's feeds have been unreliable (404 for every channel), so new videos are read from the channel's Videos tab, or the playlist's page: each one's title, thumbnail and rough upload time ("2 weeks ago"). The page shows the latest 30. Like subreddits, channels are only checked while you're using ThreadBNC, one at a time and spread across the check interval (60 minutes by default). The page doesn't carry descriptions, so when a video's post scrolls into view in the feed, the video's own page is read for its description and likes. That happens once an hour at most.

- **Comments:** opening a video's post, or showing its comments in the feed, reads its top comments the way the video's page loads them: two pages (about 40), plus the first replies to the first three threads that have some. That's the page and up to five small requests. They aren't read again within five minutes. Times ("2 days ago") and like counts over a thousand ("14K") are as rough as YouTube gives them. Comments show but can't be voted on or replied to here.

- **Videos are saved only for posts you keep.** Opening a post or scrolling past it downloads nothing. This covers any kept post that links to a YouTube video, a Lemmy or Reddit one included. [yt-dlp](https://github.com/yt-dlp/yt-dlp) does the downloading, with deno (installed by `requirements.txt`) solving YouTube's player challenges. With ffmpeg, video and sound are joined up to the chosen quality. Without it, YouTube often only has 360p as a single file.
- **The YouTube page** (`/youtube`, linked from Accounts) sets the largest video downloaded (2000 MB by default) and the **resolution** videos are saved at (1080p). They're saved as YouTube encoded them and aren't otherwise transcoded: the **Videos** media settings are for other videos. Lowering the resolution scales videos already saved at more than it down to it, in the background, at a steady quality (H.264, CRF 23); raising it doesn't bring back what was scaled down. "720p" is the shorter side, so upright videos and Shorts count the same. A community that doesn't archive videos saves no YouTube videos.
- **Session:** YouTube often asks servers to "confirm you're not a bot". On the YouTube page, paste your browser's youtube.com cookies (a `cookies.txt` export or a `Cookie` header) and, optionally, a PO token. They're encrypted like account tokens and used only for these downloads. Saving a session retries videos that failed. A spare Google account is safest: YouTube can suspend accounts it thinks are downloading.

## Accounts and posting

On the **Accounts** page, add any Lemmy or PieFed account: the server, username, password and, on Lemmy, a 2FA code if you use one. You can then act as that account from ThreadBNC. The header has a switcher for choosing which account to act as; the default is marked on the Accounts page.

| Where | What you can do |
|---|---|
| Thread page | Comment, reply to any comment, and upvote or downvote the post and each comment |
| Your own posts and comments | Edit, and delete or restore |
| Community page | **✎ New post** for a link and/or text post |
| Communities page | **＋ Start a community** on a server where one of your accounts is an admin |

**Starting a community**
- Accounts that are admins of their server get an **admin** badge. The check runs at login; use **Refresh** on the Accounts page after a change.
- A community you start is created on that server with you as moderator. You can make it NSFW, or let only moderators post.
- ThreadBNC then follows it with no expiry, so everything posted there is kept. Post with **✎ New post**.

**What's stored**
- Your password is sent to the account's server once, to log in, and is never stored.
- ThreadBNC keeps the session token the server returns, encrypted with `THREADBNC_CREDENTIALS_KEY`.
- If that setting is empty, a key is generated in the data directory, separate from the database. A database dump or backup on its own can't be used to post as you.
- Removing an account ends its session on the server too. If a server ends a session, the account is marked **needs login**; add it again to refresh the token.

**How writes work**
- Every action happens on the account's own server, the only place its token is valid.
- Targets are looked up there by their ActivityPub id. If that server hasn't seen them yet, it fetches them over federation first.

**How your own activity is archived**
- Posts you create are kept automatically.
- Your comments appear in the archive straight away, without waiting for them to reach the community's server.
- A post you create is first re-checked on your own server. Once it reaches the community's server, it's re-checked there instead, because that's where all its comments and moderation actions arrive.
- Federation delay can't create false history:
  - A comment only counts as missing if it was once seen on the server being checked.
  - A copy older than the latest known edit is ignored, not recorded as a revert.
  - A deletion you make is recorded as "deletion requested" until the community's server shows it deleted.
- Edits and deletions of your own content follow the same rules as everyone else's: earlier versions stay in the archive's history.

### Inbox

The **Inbox** collects replies to your posts and comments, mentions, and private messages, for every account on the
Accounts page, including your Reddit account. The header shows how many are unread.

- **Checking:** the bouncer checks each account every 5 minutes (`THREADBNC_INBOX_POLL_MINUTES`), and Reddit every 10 at most. **↻ Check now** checks straight away. Each check fetches about the newest 50 items; older ones keep the read state they had when last seen.
- **Read state is the server's.** Marking something read (or unread) here marks it on the account's server, and anything you read in another app shows as read here after the next check. **Mark all read** does the same for every account, or for the one you're viewing.
- **Replying:**
  - A reply goes under the comment or post, or a private message goes back to its sender. It's sent as the account the item came to, and the item is marked read.
  - This works whether or not the thread is in the archive. When it is, your reply appears there straight away. Otherwise **☆ Keep thread** saves the thread.
  - A Reddit reply needs a Reddit connection that can write.
- **Where it's from:** Lemmy 0.19 and PieFed use their replies, mentions and messages lists. Lemmy 1.0 uses its notifications. Reddit uses its message inbox. Messages you sent aren't shown.
- Removing an account removes its inbox here too. Nothing is deleted on the server.

### Moderating and administering

Moderation tools appear when the account you're posting as can use them. That means it's listed as a moderator of the community, or it's an admin of the community's home server. The moderator list is refreshed every time the bouncer checks a thread, and additions and removals are kept as community history.

| Where | Tools |
|---|---|
| Thread page, **Mod** menu on each post and comment | Remove (with a reason) or restore. For posts, also lock or unlock comments and pin or unpin in the community. **Ban the author** from the community, for N days or permanently, optionally removing their content. |
| Community page, **🛡 Moderation** tab | List moderators, add one (`user@instance`) or remove one. Ban and unban people. See a log of moderation done through ThreadBNC. |
| **Admin** page (admin accounts only) | Sign-up mode: closed, application or open. Server-wide bans. **Blocked instances** (defederation). **Blocked link domains**. |

**How these appear in the archive**
- Removals, locks and pins show as "requested via ThreadBNC" until the bouncer sees the change on the community's server.
- The observed removal then carries the modlog's reason and moderator.
- Removed content stays readable in the archive.

**Lemmy vs PieFed**
- Lemmy has no API that lists a community's bans, so the Moderation tab shows the bans made through ThreadBNC, each with an unban button. PieFed can list them, so the tab shows its list.
- Server-wide blocklists (instances and link domains) are Lemmy-only; PieFed's API doesn't expose them.
- Server-wide bans can't be listed on PieFed, so the Admin page's log shows the bans made through ThreadBNC instead.

### Your own identity (e.g. `dave@dyslectric.dev`)

To post under your own domain, run a single-user PieFed or Lemmy server for it, then add that account on the Accounts page like any other. Things to know before setting one up:

- **The server has to be on the exact domain in the handle.** `dave@dyslectric.dev` means the server runs at `https://dyslectric.dev`, not on a subdomain. Neither Lemmy nor PieFed can use a different domain in the handle from the one it runs on. If the bare domain already hosts a website, one of the two has to move.
- **One server per domain.** Lemmy and PieFed each serve one domain, so `dyslectric.dev` and `consort.chat` need a server each.
- **Close registrations** once your account exists.

## Deploy on a server (Docker Compose + Postgres)

You need a Linux server with Docker and a domain name pointing at it.

```bash
git clone https://github.com/<github-user>/threadbnc.git && cd threadbnc
cp .env.example .env
```

Edit `.env`:

- Set `THREADBNC_PASSWORD` and `POSTGRES_PASSWORD`. Generate each with `openssl rand -hex 32`.
- To use the image CI publishes instead of building on the server, set `THREADBNC_IMAGE=ghcr.io/<github-user>/threadbnc:latest`.

Then start it:

```bash
docker compose up -d
```

This starts two containers:

| Container | What it holds |
|---|---|
| `db` | Postgres 17. Data lives in the `db` volume. |
| `app` | The web UI with the bouncer built in. Archived images and the session secret live in the `media` volume. |

The UI listens only on `127.0.0.1:8080`, so nothing is exposed until you put a TLS reverse proxy in front of it. For example, with [Caddy](https://caddyserver.com/):

```
archive.example.org {
    reverse_proxy 127.0.0.1:8080
}
```

`THREADBNC_HTTPS_ONLY` defaults to `1` in the compose file, so login cookies are only sent over HTTPS. Set it to `0` only when testing over plain HTTP.

**Updating**

- If you use the published image: `docker compose pull && docker compose up -d`
- If you build on the server: `git pull && docker compose up -d --build`

The schema migrates itself on startup.

**Backups**

Back up both the database and the media volume. The media volume also holds the generated keys; without the credentials key, you'd need to log in to each account again.

```bash
docker compose exec -T db pg_dump -U threadbnc threadbnc | gzip > threadbnc-$(date +%F).sql.gz
docker run --rm -v threadbnc_media:/data -v "$PWD":/backup alpine tar czf /backup/media-$(date +%F).tgz -C /data .
```

**Verify a restore, not just the backup files**

The safest test uses a separate Compose project, so it cannot overwrite the live database or media volume. From a
temporary copy of `compose.yaml` and `.env`, choose another host port and project name:

```bash
export COMPOSE_PROJECT_NAME=threadbnc_restore_test
export THREADBNC_PORT=18080
docker compose up -d db
gzip -dc /path/to/threadbnc-YYYY-MM-DD.sql.gz \
  | docker compose exec -T db psql -v ON_ERROR_STOP=1 -U threadbnc threadbnc
docker run --rm -v threadbnc_restore_test_media:/data -v /path/to/backups:/backup alpine \
  tar xzf /backup/media-YYYY-MM-DD.tgz -C /data
docker compose run --rm app python -m threadbnc integrity --deep
docker compose up -d app
curl -fsS http://127.0.0.1:18080/healthz
test "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/)" = "303"
docker compose down -v
```

The integrity command exits nonzero for database inconsistency, a missing or size-mismatched media file, or a bad
checksum. It reports unreferenced files as warnings. After it passes, the health check and login redirect confirm
that the restored application starts and remains private. Keep the test project's name distinct from the live
project; `docker compose down -v` deletes only the test volumes in this example.

## CI

`.github/workflows/ci.yml` runs on every push and pull request:

1. Runs the test suite twice, once on SQLite and once on Postgres.
2. Builds the Docker image and starts the full compose stack. It checks that `/healthz` answers and that the archive pages require a login.
3. On pushes to `main` and on `v*` tags, publishes the image to GitHub Container Registry as `ghcr.io/<owner>/<repo>`. The tags are `latest`, `<version>` and `sha-<commit>`.

GHCR packages start out private. To let a server pull without logging in, make the package public under the repo's **Packages** settings. Otherwise run `docker login ghcr.io` on the server first.

## Run locally (development)

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:THREADBNC_PASSWORD = "choose-something-long"
.\.venv\Scripts\python.exe -m threadbnc serve          # UI on http://127.0.0.1:8080, bouncer embedded
```

- Without `THREADBNC_DATABASE_URL` set, it uses a local SQLite file in `./data`.
- Point `THREADBNC_DATABASE_URL` at Postgres to use Postgres instead.
- To run the tests against Postgres, set `THREADBNC_TEST_DATABASE_URL=postgresql://...` first. Each test gets a fresh schema in that database.

Other commands:

| Command | What it does |
|---|---|
| `python -m threadbnc bouncer` | Run only the worker (set `THREADBNC_EMBEDDED_BOUNCER=0` on the UI process) |
| `python -m threadbnc archive URL` | Archive a thread from the shell |
| `python -m threadbnc follow '!name@host' --every 15 --keep-days 30` | Follow a community |
| `python -m threadbnc sync` | Run one bouncer pass and exit |
| `python -m threadbnc integrity [--deep]` | Check database relationships and media files; `--deep` hashes every file |
| `python -m threadbnc export archive.zip` | Write a credential-free portable archive |
| `python -m threadbnc verify-export archive.zip` | Verify a portable archive's manifest and every SHA-256 checksum |
| `python -m pytest` | Run the tests |

API (each call with `Authorization: Bearer $THREADBNC_API_TOKEN`):

- `POST /archive` with `{"url": "..."}` returns a job id. Check its progress at `GET /api/jobs/{id}`.
- `GET /api/search?q=...` searches the archive and returns JSON. It takes the same filters as the search page: `what` (`post` or `comment`), `community` (its id), `author`, `kept=1`, `only` (`gone` or `edited`), `sort` (`new` or `old`) and `page`.

### Configuration (environment)

| Variable | Default | |
|---|---|---|
| `THREADBNC_PASSWORD` | *(required)* | UI login. The server refuses to start without it, unless a proxy signs people in (below); then it's an optional fallback. |
| `THREADBNC_API_TOKEN` | unset | Enables bearer-token API access |
| `THREADBNC_DATABASE_URL` | unset | `postgresql://user:pass@host/db`. Unset means SQLite in the data dir. |
| `THREADBNC_DATA_DIR` | `./data` (`/data` in Docker) | SQLite DB (if used), archived media, generated session secret |
| `THREADBNC_SECRET_KEY` | generated | Signs login sessions. If unset, a random key is created in the data dir. |
| `THREADBNC_CREDENTIALS_KEY` | generated | Encrypts stored account tokens. If unset, a key is created in the data dir. Changing it means logging in to each account again. |
| `THREADBNC_TRASH_DAYS` | `30` | Default days before trashed threads are permanently deleted (`forever` allowed) |
| `THREADBNC_HTTPS_ONLY` | `0` (`1` in compose) | Only send login cookies over HTTPS |
| `THREADBNC_FOLLOW_POLL_MINUTES` | `15` | Default check interval for Lemmy and PieFed communities checked on a schedule (only those you turn it on for) |
| `THREADBNC_FOLLOW_RETENTION_DAYS` | `30` | Default retention for auto-captured posts (`forever` allowed) |
| `THREADBNC_MIN_REQUEST_INTERVAL` | `1.0` | Seconds between requests to the same server. A server that answers 429 Too Many Requests isn't contacted again until its `Retry-After` has passed (a minute, doubling, when it doesn't say) |
| `THREADBNC_REDDIT_POLL_MINUTES` | `60` | Default check interval for followed subreddits (at least 10) |
| `THREADBNC_REDDIT_MIN_REQUEST_INTERVAL` | `2.0` | Seconds between requests to Reddit (at least 1) |
| `THREADBNC_RSS_POLL_MINUTES` | `60` | Default check interval for followed feeds (at least 5) |
| `THREADBNC_INBOX_POLL_MINUTES` | `5` | How often each account's inbox is checked (Reddit's: at least 10) |
| `THREADBNC_MEDIA_DIR` | `<data>/media` | Where archived images/videos are stored |
| `THREADBNC_MEDIA_MAX_MB` | `25` | Largest picture, video or audio file archived as it is; bigger files are skipped and linked to the original (the Storage page and each community can set this for each kind of file) |
| `THREADBNC_MEDIA_TRANSCODE` | `0` | `1`: pictures and videos over the size limit are shrunk to fit it with ffmpeg instead of skipped, unless the Storage page or a community says otherwise |
| `THREADBNC_MEDIA_TRANSCODE_SOURCE_MAX_MB` | `1000` | Largest original downloaded to transcode; bigger files are skipped |
| `THREADBNC_RELAY_INBOXES` | *(none)* | Your own Lemmy servers whose inboxes are routed through ThreadBNC, as `domain=Lemmy's address`, comma-separated (see [Pushes from your own server](#pushes-from-your-own-server)) |
| `THREADBNC_ARTICLES` | `1` | Read the web pages posts link to and keep the article, for **Read article** (see [Linked articles](#linked-articles)); `0` turns it off |
| `THREADBNC_PROXY_AUTH_HEADER` | unset | Header in which a signing-in reverse proxy passes the user's name, e.g. `X-authentik-username` |
| `THREADBNC_PROXY_SECRET` | unset | Required with the above (16+ characters). The proxy must send it as `X-ThreadBNC-Proxy-Secret` |
| `THREADBNC_PROXY_ALLOWED_USERS` | unset | Comma-separated usernames allowed in; unset = whoever the proxy lets through |
| `THREADBNC_PROXY_LOGOUT_URL` | `/outpost.goauthentik.io/sign_out` | Where Log out sends proxy-signed-in sessions, so the proxy session ends too |

#### Behind a proxy that signs people in (Authentik forward auth)

With `THREADBNC_PROXY_AUTH_HEADER` set, a request counts as signed in when it carries that header **and**
`X-ThreadBNC-Proxy-Secret: $THREADBNC_PROXY_SECRET`. The proxy adds the secret only to requests it has
authenticated, so nobody who reaches the app another way (another container on the same network, a
misrouted port) can sign in by sending the user header themselves. A session started this way lasts only
while requests keep arriving through the proxy. For Traefik: put the forward-auth middleware first and a
`headers.customrequestheaders.X-ThreadBNC-Proxy-Secret` middleware after it on the app's router. Keep API
scripts working by giving `/archive` and `/api/` requests that carry `Authorization: Bearer …` their own
router without forward auth (and without the secret); ThreadBNC's API token protects them.

## How it works

```
threadbnc/
  adapters/      ThreadiverseAdapter + LemmyAdapter (/api/v3) + PieFedAdapter (/api/alpha) + RedditAdapter
                 + RssAdapter (RSS/Atom, HTML -> Markdown)
  reddit.py      Reddit connection: app credentials, OAuth login, tokens, rate limits
  store.py       append-only persistence: revisions, state events, missing detection, purge
  bouncer.py     ingestion, source selection, sync, follows, expiry, job queue, worker loop
  feed.py        feed queries: sorting, unread / new-comment counts, thumbnails
  dupes.py       duplicate recognition: link/text keys for posts, squashing repeated comments
  search.py      full-text search over every revision (SQLite FTS5 / Postgres GIN), query syntax, snippets
  inbox.py       replies, mentions and messages per account: polling, read state, replying
  render.py      Markdown -> sanitised HTML, archived-media substitution
  media.py       media download, per-community archiving settings, content-addressed storage, cleanup
  storage.py     the Storage page's breakdown of space used
  transcode.py   ffmpeg: shrink oversized videos/pictures to fit a size limit
  web.py         FastAPI UI/API, auth guard, views
```

**Identity.** Objects are keyed by their ActivityPub id. Server-local API ids go in `object_local_ids`, keyed by domain. Communities with the same name on different instances stay separate.

**Source selection.** A post that arrived by push is read from your own server, which has everything the community sent it. A post captured by checking a community is read from where it was checked. A post you keep by link is read from the community's home instance when it can be resolved there, since that instance relays every comment and holds the moderation record; otherwise from the author's instance, then the instance you linked.

**Revisions.** A new revision is written only when the hash of title, body, URL and metadata changes. Scores and counts are stored as current values, not revisions.

**Withheld content.** Lemmy blanks the text of deleted and removed items, and account deletion overwrites it with `*Permanently Deleted*`. Neither counts as an edit. The last observed text is kept, and the event records that the server withheld it.

**State events.** These are recorded separately: author deletion and restoration, removal and restoration, lock and unlock, missing and reappeared, discovered, community removed or deleted, and instance unavailable or recovered.

- For removals and locks, the bouncer checks the modlog. It records the moderator and reason, and attributes the action to `moderator` or `admin` only when that can be confirmed; otherwise the attribution is `unknown`.
- A state already present when an object is first observed is flagged as such.

**Outages.** A network or 5xx failure never counts as a deletion. It is recorded as an instance event and retried with exponential backoff, up to 24 h. A comment that stops appearing in a complete fetch is marked `missing`, with the cause left unknown.

## Followed communities and retention

Following a community sets two things:

- **How posts arrive**: Lemmy and PieFed communities are pushed through your own server. For one that can't be, turn on **Check it on a schedule** in its **Following** menu (or tick the box when following, or `follow --poll`), and set the **check interval**. Feeds and subreddits are always checked. Each check reads further pages until it reaches posts it already has (up to 5 pages), so busy communities don't lose posts between checks. Communities followed before checking became opt-in keep being checked.
- **Retention**: how long *auto-captured* posts are kept (N days, or forever), comments and all.

New posts are captured as they arrive: the post itself, with its pictures. Its comments are read when you open it (and arrive as they're made in a pushed community). When their retention period ends, they are purged along with their comments. Along with the trash (below), this is the only way anything gets deleted:

- `purge_thread` refuses to delete a kept thread unless it is in the trash, and a test covers this.
- "Keep permanently", or archiving the same post by URL, turns an auto-captured thread into a kept one.
- Changing a community's retention recalculates expiry dates of threads already captured.
- Unfollowing stops new captures. Already captured threads keep their expiry dates.

The community page has a **Live feed** tab, fetched from the remote server on demand, with a one-click **Keep** button. Posts in the live feed are not stored unless kept or auto-captured.

## Pushes from your own server

If you run your own Lemmy server, ThreadBNC can get a followed community's changes as they happen instead of polling for them. Your account on that server subscribes to the community, the community's home server pushes everything that happens in it to your server over ActivityPub, and your server's inbox is routed through ThreadBNC on the way.

- **What arrives at once:** new posts and comments, edits, deletions and restorations, removals, locks and pins. A new post in a followed community is captured the moment it's posted. Without pushes, a Lemmy or PieFed community brings nothing new unless you have it checked on a schedule.
- **More complete:** each edit is kept, even when the next one follows seconds later, and a comment deleted soon after it was posted keeps its text, because the text comes from the delivery itself.
- **How it works:** Traefik sends POSTs to your server's inboxes (`/inbox`, `/site_inbox`, `/u/…/inbox`, `/c/…/inbox`) to ThreadBNC. ThreadBNC passes each one unchanged to Lemmy and returns Lemmy's answer to the sender. Lemmy checks the signature, and only what it accepts is kept for the push worker. The worker re-reads the post or comment from your server, where it has just arrived, and records it like a polled check. Posts captured this way are read from your server from then on, votes included, so nothing is asked of the community's home server.
- **Subscribing:** once an account on a relayed server is added, following a Lemmy or PieFed community subscribes it there too, and unfollowing unsubscribes it. The **Following** menu on a community page shows whether it's **Pushed**, and has **Get pushes** and **Stop pushes**. For communities you followed earlier, use **Subscribe to all** on the Communities page. A subscription shows **push pending** until the community accepts it (for a private community, until a moderator does). Every half hour ThreadBNC asks your server whether it has been accepted yet, and it counts as pushed as soon as a change from that community arrives.
- **Looked over, rarely:** every 6 hours, ThreadBNC reads a pushed community as your own server has it, to catch anything a delivery missed. That asks nothing of the community's home server.
- **If ThreadBNC is down,** deliveries to your server fail, and senders retry them later (Lemmy keeps retrying for a while), so the pushes arrive when ThreadBNC is back. Your server's incoming federation also pauses meanwhile.
- **Only your own servers:** pushes go to the server of the subscribing account. For an account on someone else's server, they arrive there, and ThreadBNC can't see them.

To set it up, list the server in `THREADBNC_RELAY_INBOXES` as `domain=Lemmy's address` (comma-separated for several), add a Traefik router that sends that domain's inbox POSTs to ThreadBNC with a higher priority than Lemmy's own router, and add your account on that server on the Accounts page:

```yaml
  app:
    environment:
      THREADBNC_RELAY_INBOXES: dyslectric.dev=http://lemmy-dyslectric:8536
    labels:
      traefik.http.routers.dyslectric-inbox.rule: >-
        Host(`dyslectric.dev`) && Method(`POST`)
        && (PathRegexp(`^/(site_)?inbox$$`) || PathRegexp(`^/(u|c)/[^/]+/inbox$$`))
      traefik.http.routers.dyslectric-inbox.priority: "120"
      traefik.http.routers.dyslectric-inbox.entrypoints: websecure
      traefik.http.routers.dyslectric-inbox.tls: "true"
      traefik.http.routers.dyslectric-inbox.tls.certresolver: letsencrypt
      traefik.http.routers.dyslectric-inbox.service: threadbnc
```

ThreadBNC must be able to reach Lemmy's address (here, over a shared Docker network), and it calls your server's API without the usual per-server spacing.

## Archive integrity and portable exports

The **Storage → Check archive integrity** page runs a read-only consistency check. The quick check verifies the
database, relationships between archived rows, media paths and file sizes. **Verify every media checksum** also
reads every archived file and compares its SHA-256 hash, which is slower but appropriate after restoring a backup
or moving storage. Files on disk that no archived row uses are reported as warnings; missing, changed or unsafe
files and inconsistent database rows are errors. The command-line equivalent is:

```bash
python -m threadbnc integrity --deep
```

**Portable export** on the same page downloads a ZIP intended for long-term access and interchange. It contains:

- UTF-8 JSON Lines under `data/` for communities, threads, posts, comments, every revision and state event,
  articles, media metadata and custom feeds;
- archived files under `media/`, using their content-addressed paths;
- `subscriptions.opml`, `manifest.json`, a format README and `SHA256SUMS` covering every other member.

The export intentionally excludes account tokens, Reddit and YouTube credentials, inbox state, private-community
membership, pending jobs and application secrets. It is therefore portable and safer to store, but it is not a
drop-in operational backup. Keep the Postgres/SQLite database and data directory backups described above when you
want to restore a running ThreadBNC instance exactly as it was.

Create and verify the same bundle without the browser:

```bash
python -m threadbnc export threadbnc-portable.zip
python -m threadbnc verify-export threadbnc-portable.zip
```

Verification rejects unsafe or duplicate ZIP member names, checks the format manifest, verifies every recorded
SHA-256 digest, and fails if the exporter had to omit a missing media file.

## Trash (unkeeping)

- **Unkeep → trash** works on kept threads, and **Discard → trash** on auto-captured ones.
- A trashed thread is hidden from communities, the home page and Changes, and the bouncer stops checking it.
- After the trash period, it is permanently deleted along with any media only it used. The period defaults to 30 days and is set on the Trash page: 1 day to 1 year, or "until I empty it". Changing it updates threads already in the trash.
- **Restore** puts a thread back where it was. An auto-captured thread whose retention period ran out while it was trashed comes back as kept.
- Archiving the URL of a trashed thread also restores it as kept.
- **Delete now** and **Empty trash** ask you to type `delete` to confirm.
- `THREADBNC_TRASH_DAYS` sets the default period until you change it in the UI.

## Media and Markdown

Posts and comments are rendered as Markdown: CommonMark plus tables, strikethrough, spoilers and bare-URL links. Raw HTML in content is shown as text. Output is cleaned with nh3 before display.

**What gets archived:**
- images and GIFs embedded in posts and comments
- links that point directly at image or video files (Imgur `.gifv` becomes `.mp4`)
- a post's own link, when it turns out to be an image or video

**How it works:**
- Each new revision registers its media. The bouncer downloads pictures (and video thumbnails) in the background, with retries. Full videos wait until you open or keep a post showing them; most scroll past unwatched, and they're the biggest files by far.
- Files are stored under `data/media/`, named by their SHA-256 hash, so identical files are kept once.
- Pages show the archived copy, so images survive deletion upstream. Older revisions keep their images too.
- Downloads refuse private or loopback addresses, cap redirects and file size, and check file types from their first bytes.
- `/media/{id}` requires sign-in and is served sandboxed. SVGs are never shown inline.
- Media is deleted only when every object that referenced it has been purged, which happens only when auto-captured threads expire.

**Settings for each kind of file.** Pictures, videos and audio each have their own:
- **Archive**: yes or no. Files already archived stay when you turn this off.
- **Kept as they are up to**: the largest file saved unchanged.
- **Bigger ones** (pictures and videos): leave them out, or **transcode down to** a size of their own (blank: the size they're kept as they are up to). Videos and animated GIFs become H.264/AAC MP4s at whatever bitrate fits (up to 1080p). Videos can instead be **re-encoded at a bitrate** (in Mbps, picture and sound together), which keeps the quality the same however long they are, so long ones can still come out big; videos already at or under it are kept as they are. Pictures are scaled down and saved as WebP. Videos too long to fit at a watchable bitrate are left out. Audio isn't transcoded: files over its limit are left out.

**Defaults** (the **Media defaults** section of the Storage page): these settings for every community that hasn't chosen its own. Each one left on "Server setting" (or blank) follows the `THREADBNC_MEDIA_*` environment variables. Saving retries anything the old settings left out or found too large.

**Changing settings converts what's already archived.** After you save, on the Storage page or a Media tab, the bouncer goes through the archived pictures and videos in the background. Those now over the size they're kept as they are up to, with a size to transcode down to, are transcoded from the stored copy, one at a time, and the smaller file replaces the bigger one. Files of a kind you stop archiving aren't deleted. Raising a limit doesn't bring back originals that were already transcoded.

**Pictures for browsing** (the **Pictures for browsing** section of the Storage page): the feed shows smaller copies of archived pictures, from posts and from linked articles, so browsing doesn't send phone photos and article pictures at full size. There's a width for each way the feed shows them: thumbnails in the list view (320 px by default), tiles in the grid view (640 px) and the pictures view (1280 px); 0 turns one off. The archived pictures stay as they are and open in full from a post. Copies are WebP files made with ffmpeg under `data/media/thumbs/`: when a picture is downloaded, and for what's already archived in the background after the widths change (and on first start). Until a picture has its copy, the feed shows the picture itself. Pictures no wider than a width, GIFs (so they still move) and SVGs are always shown as they are.

**Seeing what's transcoded.** The **Transcoding** section of the Storage page says whether ffmpeg is installed, what's being transcoded right now, how far the check of archived files has got (**Check archived files now** starts one without changing settings), what was transcoded lately and what couldn't be, and why. A post's page notes under its archived file what it was transcoded from, or why it couldn't be.

**Per community** (the community's **Media** tab): the same settings, each one left on "Default" (or blank) following the Storage page.
- The tab also shows how much is archived, what was transcoded, and what couldn't be archived and why. **Try these again** retries them. Saving new settings retries anything the old settings left out or found too large.
- A file posted in several communities gets the most generous of their settings.

Transcoding needs `ffmpeg` and `ffprobe` on the `PATH`. The Docker image includes them. Transcoding runs in the bouncer's background loop, so a long video can hold up other syncing for a few minutes.

## Linked articles

When a post links to a web page, the page is read when you open or keep the post, and the article on it is kept, so you can read it here and it outlives the site's copy. Nothing is fetched for posts you only scroll past.

- Posts with a saved article get a **Read article** button: in the feed (all three views) and on the thread page. It opens the article in a clean reading view (`/t/{id}/article`) with its headline, author, date, text and pictures, and a link back to the comments.
- The article is pulled out of the page with [trafilatura](https://trafilatura.readthedocs.io/): the text, headings, lists, quotes, tables, links and pictures, without the site's menus, ads, scripts and comments. It's cleaned with nh3 like everything else.
- Its pictures are archived as the post's media, so they follow the community's media settings and are shown from the archive, never loaded from the site. They're kept out of the post's thumbnail and gallery in the feed.
- Links that aren't articles aren't tried: images and videos (archived as media instead), home pages, Reddit, YouTube and other social or video sites, and links to other Lemmy or PieFed posts.
- The page is read once, when a post linking to it is first opened or kept. Posts linking to the same page share one copy.
- **What can't be saved:** pages behind a paywall or login, sites that refuse the bouncer (it identifies itself as ThreadBNC rather than posing as a browser), pages with fewer than 80 words of text, and pages over 5 MB. The thread page says why, and offers **Try again** when the site was down or refused.
- Articles are deleted with the last thread that links to them.
- `THREADBNC_ARTICLES=0` stops reading pages; links are still noted, and read once it's turned back on.

## Privacy

- Every route except `/login`, `/robots.txt` and static files requires a session or the API token.
- Responses send `noindex`, `no-store`, `no-referrer`, a strict CSP and `frame-ancestors 'none'`.
- The session cookie is `SameSite=Strict`.
- Archived text is rendered as sanitised Markdown. Raw HTML is never passed through.
- Remote images are fetched by the server, never by your browser. The same goes for linked articles and their pictures.
- Nothing is exposed publicly. Sharing and export are left for later.

## Known limits / next steps

- PieFed moderation attribution is always `unknown` for now, because its modlog API varies between versions.
- ThreadBNC has no ActivityPub identity of its own: pushes need your own Lemmy server. Without one, Lemmy and PieFed communities have to be checked on a schedule.
- Post pin/feature state, actor profile history, search, tags and notes are not implemented.
