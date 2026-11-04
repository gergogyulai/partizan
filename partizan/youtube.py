"""Everything that talks to YouTube goes through yt-dlp in this module.

All functions here are blocking; callers run them in a worker thread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import yt_dlp
from yt_dlp.utils import DownloadError, UserNotLive

from .config import Config

log = logging.getLogger(__name__)

PLAYABLE_AVAILABILITY = (None, "public", "unlisted")


@dataclass
class Video:
    id: str
    title: str
    duration: int
    description: str = ""
    index: int = 0  # position in the channel listing, 0 = newest

    @property
    def thumbnail(self) -> str:
        return thumbnail_url(self.id)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "duration": self.duration,
            "description": self.description,
            "index": self.index,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Video":
        return cls(
            id=data["id"],
            title=data.get("title") or data["id"],
            duration=int(data["duration"]),
            description=data.get("description") or "",
            index=int(data.get("index") or 0),
        )


@dataclass
class ChannelInfo:
    name: str | None = None
    channel_id: str | None = None
    logo: str | None = None


@dataclass
class LiveInfo:
    id: str
    title: str
    description: str = ""
    started_at: float | None = None

    @property
    def thumbnail(self) -> str:
        return thumbnail_url(self.id)


@dataclass
class MediaInput:
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ResolvedMedia:
    inputs: list[MediaInput]
    is_live: bool


def thumbnail_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


class _QuietLogger:
    def debug(self, msg):  # yt-dlp routes info messages through debug as well
        log.debug("yt-dlp: %s", msg)

    def info(self, msg):
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg):
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg):
        log.debug("yt-dlp error: %s", msg)


class YouTube:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        h = cfg.height
        if cfg.mode == "copy":
            # Stream copy needs H.264 video; audio is always re-encoded.
            self.vod_format = cfg.ytdlp_format_vod or (
                f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
                f"b[height<={h}][vcodec^=avc1]/b[vcodec^=avc1]"
            )
        else:
            # Prefer H.264 sources (cheapest to decode), avoid AV1 when possible.
            self.vod_format = cfg.ytdlp_format_vod or (
                f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
                f"bv*[height<={h}][vcodec!^=av01]+ba/"
                f"b[height<={h}]/bv*+ba/b"
            )
        self.live_format = cfg.ytdlp_format_live or (
            f"b[height<={h}][protocol^=m3u8]/b[protocol^=m3u8]/b"
        )

    def _opts(self, **extra) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "logger": _QuietLogger(),
            "socket_timeout": 20,
        }
        if self.cfg.ytdlp_cookies:
            opts["cookiefile"] = self.cfg.ytdlp_cookies
        opts.update(extra)
        return opts

    # ------------------------------------------------------------------ library
    def fetch_library(self) -> tuple[ChannelInfo, list[Video]]:
        """List the channel's videos (flat, no per-video requests)."""
        info = ChannelInfo()
        videos: dict[str, Video] = {}
        errors = []
        for tab in self.cfg.library_tabs:
            url = f"{self.cfg.channel_url}/{tab}"
            opts = self._opts(extract_flat="in_playlist", playlistend=self.cfg.library_max)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    result = ydl.extract_info(url, download=False)
            except DownloadError as exc:
                log.warning("Could not list %s: %s", url, exc)
                errors.append(exc)
                continue
            if not result:
                continue
            info.name = info.name or result.get("channel") or result.get("uploader")
            info.channel_id = info.channel_id or result.get("channel_id")
            info.logo = info.logo or _pick_avatar(result.get("thumbnails") or [])
            for entry in result.get("entries") or []:
                video = self._entry_to_video(entry, len(videos))
                if video and video.id not in videos:
                    videos[video.id] = video
        if errors and not videos:
            raise errors[0]
        log.info("Library: %d playable videos from tabs %s", len(videos), self.cfg.library_tabs)
        return info, list(videos.values())

    def _entry_to_video(self, entry: dict, index: int) -> Video | None:
        if not entry or not entry.get("id"):
            return None
        if entry.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            return None
        if entry.get("availability") not in PLAYABLE_AVAILABILITY:
            return None
        duration = entry.get("duration")
        if not duration:
            return None
        duration = int(duration)
        if duration < self.cfg.min_duration:
            return None
        if self.cfg.max_duration and duration > self.cfg.max_duration:
            return None
        return Video(
            id=entry["id"],
            title=entry.get("title") or entry["id"],
            duration=duration,
            description=(entry.get("description") or "").strip(),
            index=index,
        )

    # --------------------------------------------------------------------- live
    def check_live(self) -> LiveInfo | None:
        """Return the channel's current livestream, or None if it is not live.

        Raises on errors that don't clearly mean "not live" (network trouble etc.),
        so the caller can keep its previous state instead of flapping.
        """
        url = f"{self.cfg.channel_url}/live"
        opts = self._opts(
            ignore_no_formats_error=True,
            # We only need the live status here, not the stream manifests.
            extractor_args={"youtube": {"skip": ["hls", "dash"]}},
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                result = ydl.extract_info(url, download=False, process=True)
        except UserNotLive:
            return None
        except DownloadError as exc:
            message = str(exc).lower()
            if "not currently live" in message or "will begin" in message or "premieres in" in message:
                return None
            raise
        if not result or result.get("_type") == "playlist":
            return None
        if result.get("live_status") != "is_live":
            return None
        if result.get("availability") not in PLAYABLE_AVAILABILITY:
            log.info("Channel is live but the stream is %s; ignoring", result.get("availability"))
            return None
        return LiveInfo(
            id=result["id"],
            title=result.get("title") or "Live",
            description=(result.get("description") or "").strip(),
            started_at=result.get("release_timestamp") or result.get("timestamp"),
        )

    # ------------------------------------------------------------------ resolve
    def resolve(self, video_id: str, live: bool = False) -> ResolvedMedia:
        """Turn a video id into direct media URLs that ffmpeg can read."""
        opts = self._opts(format=self.live_format if live else self.vod_format)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        formats = info.get("requested_formats") or [info]
        inputs = [
            MediaInput(url=f["url"], headers=dict(f.get("http_headers") or info.get("http_headers") or {}))
            for f in formats
            if f.get("url")
        ]
        if not inputs:
            raise DownloadError(f"No playable formats for {video_id}")
        return ResolvedMedia(inputs=inputs, is_live=info.get("live_status") == "is_live")


def _pick_avatar(thumbnails: list[dict]) -> str | None:
    for thumb in thumbnails:
        if "avatar" in str(thumb.get("id", "")) and thumb.get("url"):
            return thumb["url"]
    square = [
        t for t in thumbnails if t.get("url") and t.get("width") and t.get("width") == t.get("height")
    ]
    if square:
        return max(square, key=lambda t: t["width"])["url"]
    return None
