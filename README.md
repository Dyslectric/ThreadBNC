# ThreadBNC — Threadiverse bouncer + private archive

A private, feed-first reader for Lemmy and PieFed that never loses what it has seen.

**Following and reading:**
- Follow communities. The **bouncer** saves every new post along with its comments.
- Your **feed** is built from that saved copy. Posts you haven't opened stand out, opened posts show "N new comments", and you can sort by New, Active, Top or Most comments.
- The feed keeps working when an instance is down, and shows edits, removals and deletions as history instead of losing them.

**Keeping:**
- **☆ Keep** any post to hold it forever; unkept feed posts expire after the community's retention period.
- You can also keep a single post by pasting its link on the **Kept** page, without following its community.

> Remote state may change. Archived observations do not disappear.

## Pages

| Page | What it's for |
|---|---|
| **Feed** (`/`) | Posts from every followed community. Sort by New, Active, Top or Most comments; filter by day, week, month or all time; show unread only; mark all read. The sidebar lists followed communities with unread counts. |
| **Community** (`/c/{id}`) | The same feed for one community, plus ★ Kept, **Live on server** (browse its full history, fetched live) and a log. Follow settings sit behind the "✓ Following" pill. |
| **Communities** | Follow a community (starts with its current first page) and manage the check interval and retention for each one. |
| **★ Kept** | Keep a post by link, see kept threads grouped by community, recent changes and the bouncer queue. |
| **Trash** | Hidden and unkept threads, restorable until the trash period ends. |

**Keep and Hide:**
- **☆ Keep** holds a post forever.
- **★ Kept** unkeeps it. A post that came from a followed community goes back into the feed and expires normally. A post you kept by link goes to the trash.
- **Hide** moves a feed post to the trash.

## Run

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:THREADBNC_PASSWORD = "choose-something-long"
.\.venv\Scripts\python.exe -m threadbnc serve          # UI on http://127.0.0.1:8080, bouncer embedded
```

Other commands:

| Command | What it does |
|---|---|
| `python -m threadbnc bouncer` | Run only the worker (set `THREADBNC_EMBEDDED_BOUNCER=0` on the UI process) |
| `python -m threadbnc archive URL` | Archive a thread from the shell |
| `python -m threadbnc follow '!name@host' --every 15 --keep-days 30` | Follow a community |
| `python -m threadbnc sync` | Run one bouncer pass and exit |
| `python -m pytest` | Run the tests |

API: `POST /archive` with `{"url": "..."}` and `Authorization: Bearer $THREADBNC_API_TOKEN` returns a job id. Check its progress at `GET /api/jobs/{id}`.

### Configuration (environment)

| Variable | Default | |
|---|---|---|
| `THREADBNC_PASSWORD` | *(required)* | UI login. The server refuses to start without it. |
| `THREADBNC_API_TOKEN` | unset | Enables bearer-token API access |
| `THREADBNC_DATA_DIR` | `./data` | SQLite DB and generated session secret |
| `THREADBNC_HTTPS_ONLY` | `0` | Set `1` behind TLS so session cookies are Secure |
| `THREADBNC_SYNC_MINUTES` | `30` | Re-check interval for threads in communities you don't follow |
| `THREADBNC_FOLLOW_POLL_MINUTES` | `15` | Default poll interval for followed communities |
| `THREADBNC_FOLLOW_RETENTION_DAYS` | `30` | Default retention for auto-captured posts (`forever` allowed) |
| `THREADBNC_MIN_REQUEST_INTERVAL` | `1.0` | Seconds between requests to the same instance |
| `THREADBNC_MEDIA_DIR` | `<data>/media` | Where archived images/videos are stored |
| `THREADBNC_MEDIA_MAX_MB` | `25` | Largest single image or video file that will be archived; bigger files are skipped and linked to the original |

## How it works

```
threadbnc/
  adapters/      ThreadiverseAdapter + LemmyAdapter (/api/v3) + PieFedAdapter (/api/alpha)
  store.py       append-only persistence: revisions, state events, missing detection, purge
  bouncer.py     ingestion, source selection, sync, follows, expiry, job queue, worker loop
  feed.py        feed queries: sorting, unread / new-comment counts, thumbnails
  render.py      Markdown -> sanitised HTML, archived-media substitution
  media.py       media download, content-addressed storage, cleanup
  web.py         FastAPI UI/API, auth guard, views
```

**Identity.** Objects are keyed by their ActivityPub id. Server-local API ids go in `object_local_ids`, keyed by domain. Communities with the same name on different instances stay separate.

**Source selection.** A thread is polled from the community's home instance when it can be resolved there. That instance relays every comment and holds the moderation record. Otherwise the bouncer uses the author's instance, then the instance you linked.

**Revisions.** A new revision is written only when the hash of title, body, URL and metadata changes. Scores and counts are stored as current values, not revisions.

**Withheld content.** Lemmy blanks the text of deleted and removed items, and account deletion overwrites it with `*Permanently Deleted*`. Neither counts as an edit. The last observed text is kept, and the event records that the server withheld it.

**State events.** These are recorded separately: author deletion and restoration, removal and restoration, lock and unlock, missing and reappeared, discovered, community removed or deleted, and instance unavailable or recovered.

- For removals and locks, the bouncer checks the modlog. It records the moderator and reason, and attributes the action to `moderator` or `admin` only when that can be confirmed; otherwise the attribution is `unknown`.
- A state already present when an object is first observed is flagged as such.

**Outages.** A network or 5xx failure never counts as a deletion. It is recorded as an instance event and retried with exponential backoff, up to 24 h. A comment that stops appearing in a complete fetch is marked `missing`, with the cause left unknown.

## Followed communities and retention

Following a community sets two things:

- **Poll interval**: how often the bouncer checks the community for new posts. Threads in that community are re-checked at the same interval. Each check reads further pages until it reaches posts it already has (up to 5 pages), so busy communities don't lose posts between checks.
- **Retention**: how long *auto-captured* posts are kept (N days, or forever).

New posts are captured automatically and tracked in full: revisions, events and new comments. When their retention period ends, they are purged. Along with the trash (below), this is the only way anything gets deleted:

- `purge_thread` refuses to delete a kept thread unless it is in the trash, and a test covers this.
- "Keep permanently", or archiving the same post by URL, turns an auto-captured thread into a kept one.
- Changing a community's retention recalculates expiry dates of threads already captured.
- Unfollowing stops new captures. Already captured threads keep their expiry dates.

The community page has a **Live feed** tab, fetched from the remote server on demand, with a one-click **Keep** button. Posts in the live feed are not stored unless kept or auto-captured.

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
- Each new revision registers its media. The bouncer downloads it in the background, with retries.
- Files are stored under `data/media/`, named by their SHA-256 hash, so identical files are kept once.
- Pages show the archived copy, so images survive deletion upstream. Older revisions keep their images too.
- Downloads refuse private or loopback addresses, cap redirects and file size, and check file types from their first bytes.
- `/media/{id}` requires sign-in and is served sandboxed. SVGs are never shown inline.
- Media is deleted only when every object that referenced it has been purged, which happens only when auto-captured threads expire.

## Privacy

- Every route except `/login`, `/robots.txt` and static files requires a session or the API token.
- Responses send `noindex`, `no-store`, `no-referrer`, a strict CSP and `frame-ancestors 'none'`.
- The session cookie is `SameSite=Strict`.
- Archived text is rendered as sanitised Markdown. Raw HTML is never passed through.
- Remote images are fetched by the server, never by your browser.
- Nothing is exposed publicly. Sharing and export are left for later.

## Known limits / next steps

- Lemmy 1.0's `/api/v4` is not targeted yet. The adapter speaks v3, which 0.19.x serves.
- PieFed moderation attribution is always `unknown` for now, because its modlog API varies between versions.
- The bouncer polls; it does not listen for ActivityPub deliveries (Phase 5).
- Post pin/feature state, actor profile history, search, tags and notes are not implemented.
