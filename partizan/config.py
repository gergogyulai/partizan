"""Configuration, read from environment variables (Docker friendly)."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value is not None else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def normalize_channel_url(channel: str) -> str:
    """Accept a channel URL, an @handle or a UC... channel id and return a base URL."""
    channel = channel.strip()
    if re.fullmatch(r"UC[\w-]{22}", channel):
        return f"https://www.youtube.com/channel/{channel}"
    if channel.startswith("@"):
        return f"https://www.youtube.com/{channel}"
    if not channel.startswith("http"):
        channel = "https://" + channel.lstrip("/")
    channel = channel.split("?")[0].rstrip("/")
    # Strip a trailing tab such as /videos, /streams, /live, /featured.
    channel = re.sub(
        r"/(videos|streams|live|featured|shorts|playlists|community|about)$", "", channel
    )
    return channel


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "channel"


@dataclass
class Config:
    channel: str
    channel_url: str
    channel_name: str | None = None
    channel_logo: str | None = None
    tvg_id: str = "channel"
    channel_number: str = "1"

    host: str = "0.0.0.0"
    port: int = 8080
    base_url: str | None = None
    data_dir: Path = Path("./data")

    live_poll_interval: float = 60.0
    library_refresh_interval: float = 6 * 3600.0
    library_tabs: list[str] = field(default_factory=lambda: ["videos"])
    library_max: int = 300
    min_duration: int = 60
    max_duration: int = 0  # 0 = unlimited
    order: str = "shuffle"  # shuffle | newest | oldest

    epg_hours: float = 48.0
    epg_past_hours: float = 6.0
    live_block: float = 3600.0  # assumed length of a live stream in the guide

    mode: str = "transcode"  # transcode | copy
    width: int = 1280
    height: int = 720
    fps: int = 30
    video_bitrate: str = "3000k"
    audio_bitrate: str = "128k"
    x264_preset: str = "veryfast"
    ffmpeg_video_args: list[str] | None = None  # full override of video encoder args
    ffmpeg_path: str = "ffmpeg"

    ytdlp_format_vod: str | None = None
    ytdlp_format_live: str | None = None
    ytdlp_cookies: str | None = None

    always_on: bool = False
    idle_timeout: float = 30.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        channel = _env("CHANNEL")
        if not channel:
            raise SystemExit(
                "CHANNEL is required, e.g. CHANNEL=@SomeChannel or a channel URL / UC... id"
            )
        height = _env_int("VIDEO_HEIGHT", 720)
        width = _env_int("VIDEO_WIDTH", int(round(height * 16 / 9 / 2)) * 2)
        video_args = _env("FFMPEG_VIDEO_ARGS")
        mode = (_env("MODE", "transcode") or "transcode").lower()
        if mode not in ("transcode", "copy"):
            raise SystemExit("MODE must be 'transcode' or 'copy'")
        order = (_env("ORDER", "shuffle") or "shuffle").lower()
        if order not in ("shuffle", "newest", "oldest"):
            raise SystemExit("ORDER must be 'shuffle', 'newest' or 'oldest'")
        return cls(
            channel=channel,
            channel_url=normalize_channel_url(channel),
            channel_name=_env("CHANNEL_NAME"),
            channel_logo=_env("CHANNEL_LOGO"),
            tvg_id=_env("TVG_ID") or slugify(channel.split("/")[-1].lstrip("@")),
            channel_number=_env("CHANNEL_NUMBER", "1") or "1",
            host=_env("HOST", "0.0.0.0") or "0.0.0.0",
            port=_env_int("PORT", 8080),
            base_url=(_env("BASE_URL") or "").rstrip("/") or None,
            data_dir=Path(_env("DATA_DIR", "./data") or "./data"),
            live_poll_interval=_env_float("LIVE_POLL_INTERVAL", 60.0),
            library_refresh_interval=_env_float("LIBRARY_REFRESH_INTERVAL", 6 * 3600.0),
            library_tabs=[
                t.strip() for t in (_env("LIBRARY_TABS", "videos") or "videos").split(",") if t.strip()
            ],
            library_max=_env_int("LIBRARY_MAX", 300),
            min_duration=_env_int("MIN_DURATION", 60),
            max_duration=_env_int("MAX_DURATION", 0),
            order=order,
            epg_hours=_env_float("EPG_HOURS", 48.0),
            epg_past_hours=_env_float("EPG_PAST_HOURS", 6.0),
            live_block=_env_float("LIVE_BLOCK_MINUTES", 60.0) * 60,
            mode=mode,
            width=width,
            height=height,
            fps=_env_int("FPS", 30),
            video_bitrate=_env("VIDEO_BITRATE", "3000k") or "3000k",
            audio_bitrate=_env("AUDIO_BITRATE", "128k") or "128k",
            x264_preset=_env("X264_PRESET", "veryfast") or "veryfast",
            ffmpeg_video_args=shlex.split(video_args) if video_args else None,
            ffmpeg_path=_env("FFMPEG", "ffmpeg") or "ffmpeg",
            ytdlp_format_vod=_env("YTDLP_FORMAT"),
            ytdlp_format_live=_env("YTDLP_FORMAT_LIVE"),
            ytdlp_cookies=_env("YTDLP_COOKIES"),
            always_on=_env_bool("ALWAYS_ON", False),
            idle_timeout=_env_float("IDLE_TIMEOUT", 30.0),
            log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        )
