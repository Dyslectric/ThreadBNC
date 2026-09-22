"""Shrinking media that is too big to archive as-is, with ffmpeg.

Videos (and animated GIFs) become H.264/AAC MP4s at a bitrate worked out from
their length so the result fits the size limit; pictures are scaled down and
re-encoded as WebP (JPEG if this ffmpeg has no WebP encoder). Nothing here
touches the database: media.py decides when to call it and records the result.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from functools import cache
from pathlib import Path

TIMEOUT = 30 * 60  # seconds for one ffmpeg run; a long video on a slow box takes a while
MIN_VIDEO_BPS = 100_000  # below this a video is mush; give up instead
# (longest side, WebP quality) tried in turn until a picture fits.
IMAGE_STEPS = [(None, 85), (4096, 80), (2560, 80), (1920, 75), (1280, 70), (1024, 60)]


class TranscodeError(Exception):
    pass


@cache
def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@cache
def _has_encoder(name: str) -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30)
    return any(line.split()[1:2] == [name] for line in out.stdout.splitlines())


def _run(args: list[str], timeout: float) -> None:
    try:
        proc = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", *args],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TranscodeError(f"ffmpeg took longer than {timeout // 60:.0f} minutes") from None
    if proc.returncode != 0:
        lines = [ln for ln in proc.stderr.strip().splitlines() if ln.strip()]
        raise TranscodeError("ffmpeg: " + (lines[-1] if lines else f"exit {proc.returncode}"))


def probe(path: Path) -> dict:
    try:
        proc = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                               str(path)], capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            return json.loads(proc.stdout or "{}")
    except (subprocess.TimeoutExpired, ValueError):
        pass
    raise TranscodeError("unreadable file (ffprobe failed)")


def shrink(src: Path, content_type: str, limit: int, workdir: Path,
           timeout: float = TIMEOUT) -> tuple[Path, str]:
    """Re-encode `src` to at most `limit` bytes. Returns (new file, its content type);
    the caller owns the new file. Raises TranscodeError if it can't be made to fit."""
    if not available():
        raise TranscodeError("ffmpeg is not installed")
    if content_type.startswith("video/") or content_type == "image/gif":
        return _video(src, limit, workdir, timeout), "video/mp4"
    if content_type.startswith("image/") and content_type != "image/svg+xml":
        return _image(src, limit, workdir, timeout)
    raise TranscodeError(f"can't transcode {content_type}")


def _tmp(workdir: Path, suffix: str) -> Path:
    with tempfile.NamedTemporaryFile(dir=workdir, prefix=".tc-", suffix=suffix, delete=False) as f:
        return Path(f.name)


def _video(src: Path, limit: int, workdir: Path, timeout: float) -> Path:
    info = probe(src)
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise TranscodeError("no video stream")
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise TranscodeError("unknown length")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    total_bps = limit * 8 * 0.92 / duration  # headroom for the container
    audio_bps = (96_000 if total_bps > 600_000 else 48_000) if has_audio else 0
    video_bps = total_bps - audio_bps
    if video_bps < MIN_VIDEO_BPS:
        raise TranscodeError(f"too long ({duration / 60:.0f} min) to fit in {limit // 1_000_000} MB")
    height = 1080 if video_bps >= 2_500_000 else 720 if video_bps >= 1_000_000 else 480 if video_bps >= 400_000 \
        else 360
    out = _tmp(workdir, ".mp4")
    try:
        for factor in (1.0, 0.75, 0.55):
            bps = int(video_bps * factor)
            args = ["-i", str(src), "-map", "0:v:0", "-vf",
                    f"scale=-2:'trunc(min(ih,{height})/2)*2',format=yuv420p",
                    "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(bps), "-maxrate", str(bps),
                    "-bufsize", str(bps * 2)]
            args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", str(audio_bps)] if has_audio else ["-an"]
            _run([*args, "-movflags", "+faststart", "-f", "mp4", str(out)], timeout)
            if out.stat().st_size <= limit:
                return out
        raise TranscodeError(f"still over {limit // 1_000_000} MB after transcoding")
    except BaseException:
        out.unlink(missing_ok=True)
        raise


def _image(src: Path, limit: int, workdir: Path, timeout: float) -> tuple[Path, str]:
    webp = _has_encoder("libwebp")
    suffix, ctype = (".webp", "image/webp") if webp else (".jpg", "image/jpeg")
    out = _tmp(workdir, suffix)
    try:
        for side, quality in IMAGE_STEPS:
            args = ["-i", str(src), "-frames:v", "1"]
            if side:
                args += ["-vf", f"scale='min(iw,{side})':'min(ih,{side})':force_original_aspect_ratio=decrease"]
            if webp:
                args += ["-c:v", "libwebp", "-quality", str(quality)]
            else:  # mjpeg's -q:v runs 2 (best) to 31
                args += ["-c:v", "mjpeg", "-q:v", str(max(2, (100 - quality) // 5))]
            _run([*args, "-f", "webp" if webp else "image2", str(out)], timeout)
            if out.stat().st_size <= limit:
                return out, ctype
        raise TranscodeError(f"still over {limit // 1_000_000} MB after scaling down")
    except BaseException:
        out.unlink(missing_ok=True)
        raise
