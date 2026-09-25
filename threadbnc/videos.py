"""Video links: a link to a video on a site whose player can be embedded
(Vimeo, Dailymotion, Streamable, a PeerTube server) or to a video file opens
a box with its player, under the link (app.js), and a button to download and
archive it. A post that is such a link shows the box in the post. YouTube's
links have a box of their own (/youtube/v/<id>, web.py), and livestreams
theirs (livestream.py).

Nothing is fetched here to show the player: it's the site's own, in an
iframe, or for a file the browser's, playing it from where it is. Saving one
downloads it in the background (media.want_video): a site's video with yt-dlp
(youtube.download, at the YouTube page's resolution and size limit), a file
as other videos are (media.py, the Videos settings), whatever the community
would save. Either is kept until you delete it, and listed on the Kept page.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlparse

from .render import VIDEO_EXTENSIONS

BOX_HREF = "/video?url={}"  # the box a video link opens (web.py, app.js)

_VIMEO_HASH = re.compile(r"^[0-9a-f]{6,20}$")
_DAILYMOTION_ID = re.compile(r"^x[0-9a-z]{3,10}$", re.IGNORECASE)
_STREAMABLE_ID = re.compile(r"^[0-9a-z]{3,10}$", re.IGNORECASE)
# Streamable's own pages, not videos: streamable.com/<these>.
_STREAMABLE_PAGES = {"login", "signup", "upload", "pricing", "privacy", "terms", "blog", "community", "about",
                     "contact", "careers", "settings", "documentation", "embed", "clipper", "videos", "help"}
_PEERTUBE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PEERTUBE_SHORT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{20,23}$")  # base58
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::\d{1,5})?$")


@dataclass(frozen=True)
class Video:
    kind: str       # vimeo, dailymotion, streamable, peertube, file
    key: str        # the video's id on its site; a file's address
    host: str = ""  # a PeerTube server's name
    secret: str = ""  # an unlisted Vimeo video's hash

    @property
    def is_file(self) -> bool:
        return self.kind == "file"

    @property
    def url(self) -> str:
        """Its address, the same however it was linked: its media row's."""
        if self.kind == "vimeo":
            return f"https://vimeo.com/{self.key}" + (f"/{self.secret}" if self.secret else "")
        if self.kind == "dailymotion":
            return f"https://www.dailymotion.com/video/{self.key}"
        if self.kind == "streamable":
            return f"https://streamable.com/{self.key}"
        if self.kind == "peertube":
            return f"https://{self.host}/w/{self.key}"
        return self.key

    @property
    def site(self) -> str:
        if self.is_file:
            return urlparse(self.key).hostname or "the site"
        return {"vimeo": "Vimeo", "dailymotion": "Dailymotion", "streamable": "Streamable"}.get(self.kind, "PeerTube")

    @property
    def embed(self) -> str | None:
        """The site's player (None for a file, which the browser plays)."""
        if self.kind == "vimeo":
            return f"https://player.vimeo.com/video/{self.key}?dnt=1" + (f"&h={self.secret}" if self.secret else "")
        if self.kind == "dailymotion":
            return f"https://www.dailymotion.com/embed/video/{self.key}"
        if self.kind == "streamable":
            return f"https://streamable.com/e/{self.key}"
        if self.kind == "peertube":  # without sharing it with other viewers (WebRTC), which shows them your address
            return f"https://{self.host}/videos/embed/{self.key}?p2p=0"
        return None

    @property
    def plays_from(self) -> str | None:
        """For a file, where the browser plays it from before it's saved:
        only https (the page's policy allows no other), and imgur-style
        .gifv as the .mp4 it is."""
        if not self.is_file or not self.key.lower().startswith("https://"):
            return None
        u = urlparse(self.key)
        if u.path.lower().endswith(".gifv"):
            return u._replace(path=u.path[: -len(".gifv")] + ".mp4").geturl()
        return self.key

    @property
    def fetch_url(self) -> str:
        """What yt-dlp is given to download it. A PeerTube server isn't known
        to yt-dlp by its name, so it's told. Vimeo's own pages want you
        signed in; its player doesn't."""
        if self.kind == "peertube":
            return f"peertube:{self.host}:{self.key}"
        if self.kind == "vimeo":
            return f"https://player.vimeo.com/video/{self.key}" + (f"?h={self.secret}" if self.secret else "")
        return self.url

    @property
    def href(self) -> str:
        return BOX_HREF.format(quote(self.url, safe=""))

    @property
    def box_id(self) -> str:
        """The box's element id: the same for every link to it."""
        return f"video-{self.kind}-" + hashlib.sha1(self.url.encode()).hexdigest()[:12]

    @property
    def label(self) -> str:
        """What to call it, with no title: a file's name, else its address."""
        if self.is_file:
            return unquote(urlparse(self.key).path.rsplit("/", 1)[-1]) or self.key
        return self.url.split("://", 1)[1]

    @property
    def link_title(self) -> str:
        return f"{self.site} video: watch it here, or download and archive it" if not self.is_file else \
            "Video file: watch it here, or download and archive it"


def video_of(url: str | None) -> Video | None:
    """The video a link is to, if its address says so. YouTube's aren't
    these (youtube.video_id), nor are livestreams (livestream.stream_of)."""
    if not url:
        return None
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        return None
    host = u.hostname.lower()
    parts = [p for p in u.path.split("/") if p]
    if host in ("vimeo.com", "www.vimeo.com", "player.vimeo.com"):
        return _vimeo(host, parts, parse_qs(u.query))
    if host in ("dailymotion.com", "www.dailymotion.com", "dai.ly"):
        # dai.ly/<id>, dailymotion.com/video/<id>_<its title>, /embed/video/<id>
        if host == "dai.ly":
            key = parts[0] if len(parts) == 1 else ""
        else:
            key = parts[-1].split("_")[0] if parts[:-1] in (["video"], ["embed", "video"]) else ""
        return Video("dailymotion", key) if _DAILYMOTION_ID.match(key) else None
    if host in ("streamable.com", "www.streamable.com"):
        if len(parts) == 2 and parts[0] in ("e", "o", "s"):
            parts = parts[1:]
        if len(parts) == 1 and _STREAMABLE_ID.match(parts[0]) and parts[0].lower() not in _STREAMABLE_PAGES:
            return Video("streamable", parts[0])
        return None
    peertube = _peertube(parts)
    if peertube and _HOST.match(u.netloc.lower()):
        return Video("peertube", peertube, host=u.netloc.lower())
    if u.path.lower().endswith(VIDEO_EXTENSIONS):
        return Video("file", url)
    return None


def _vimeo(host: str, parts: list[str], query: dict[str, list[str]]) -> Video | None:
    if host == "player.vimeo.com":
        if len(parts) == 2 and parts[0] == "video" and parts[1].isdigit():
            secret = query.get("h", [""])[0]
            return Video("vimeo", parts[1], secret=secret if _VIMEO_HASH.match(secret) else "")
        return None
    if parts and parts[0].isdigit():  # vimeo.com/<id>, or /<id>/<hash> when it's unlisted
        if len(parts) == 1:
            return Video("vimeo", parts[0])
        if len(parts) == 2 and _VIMEO_HASH.match(parts[1]):
            return Video("vimeo", parts[0], secret=parts[1])
        return None
    # vimeo.com/channels/<name>/<id>, /groups/<name>/videos/<id>, /showcase/<id>/video/<id>, /album/…
    if len(parts) == 3 and parts[0] == "channels" and parts[2].isdigit():
        return Video("vimeo", parts[2])
    if len(parts) == 4 and parts[0] in ("groups", "showcase", "album") and parts[2] in ("videos", "video") \
            and parts[3].isdigit():
        return Video("vimeo", parts[3])
    return None


def _peertube(parts: list[str]) -> str | None:
    """A PeerTube video's id from its page's path: /w/<id>, /videos/watch/<id>
    or /videos/embed/<id> (not a playlist's: /w/p/<id>)."""
    if len(parts) == 2 and parts[0] == "w":
        key = parts[1]
    elif len(parts) == 3 and parts[0] == "videos" and parts[1] in ("watch", "embed"):
        key = parts[2]
    else:
        return None
    return key if _PEERTUBE_UUID.match(key) or _PEERTUBE_SHORT.match(key) else None
