"""Glue between YouTube, the schedule and the stream pipeline."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from .config import Config
from .schedule import Schedule
from .streamer import Broadcaster, Player, Source
from .youtube import ChannelInfo, LiveInfo, ResolvedMedia, Video, YouTube

log = logging.getLogger(__name__)

RESOLVE_TTL = 3 * 3600  # googlevideo URLs are valid for ~6h
FAILURE_WINDOW = 120
MAX_QUICK_FAILURES = 3
UNPLAYABLE_LIVE_COOLDOWN = 10 * 60
LIVE_RECHECK_DELAY = 5  # seconds to wait after an on-demand live check request


class Channel:
    def __init__(self, cfg: Config, yt: YouTube | None = None):
        self.cfg = cfg
        self.yt = yt or YouTube(cfg)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.library_path = cfg.data_dir / "library.json"
        self.schedule = Schedule(
            order=cfg.order,
            horizon=cfg.epg_hours * 3600,
            history=cfg.epg_past_hours * 3600,
            live_block=cfg.live_block,
            path=cfg.data_dir / "schedule.json",
        )
        self.info = ChannelInfo()
        self.live: LiveInfo | None = None
        self.broadcaster = Broadcaster(
            on_first=lambda: self.player.ensure_running(),
            on_last=lambda: self.player.stop_later(),
        )
        self.player = Player(cfg, self, self.broadcaster)

        self._live_event = asyncio.Event()
        self._check_now = asyncio.Event()
        self._resolved: dict[tuple[str, bool], tuple[ResolvedMedia, float]] = {}
        self._resolving: dict[tuple[str, bool], asyncio.Task] = {}
        self._failures: dict[str, list[float]] = {}
        self._unplayable_live: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []

        self.last_live_check: float | None = None
        self.live_check_error: str | None = None
        self.last_library_refresh: float | None = None
        self.library_error: str | None = None

    # ----------------------------------------------------------- properties
    @property
    def name(self) -> str:
        return self.cfg.channel_name or self.info.name or self.cfg.tvg_id

    @property
    def logo(self) -> str | None:
        return self.cfg.channel_logo or self.info.logo

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._load_library_cache()
        self.schedule.load()
        self.schedule.ensure()
        self._tasks = [
            asyncio.create_task(self._library_loop(), name="library"),
            asyncio.create_task(self._live_loop(), name="live"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
        ]
        if self.cfg.always_on:
            self.player.ensure_running()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.broadcaster.close_all()
        await self.player.stop()
        self.schedule.save()

    # -------------------------------------------------------------- library
    def _load_library_cache(self) -> None:
        if not self.library_path.exists():
            return
        try:
            data = json.loads(self.library_path.read_text())
            self.info = ChannelInfo(**data.get("channel", {}))
            videos = [Video.from_dict(v) for v in data.get("videos", [])]
            self.schedule.library = {v.id: v for v in videos}
            log.info("Loaded %d cached videos", len(videos))
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("Ignoring unreadable library cache: %s", exc)

    def _save_library_cache(self, videos: list[Video]) -> None:
        data = {"channel": vars(self.info), "videos": [v.to_dict() for v in videos]}
        tmp = self.library_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.library_path)

    async def refresh_library(self) -> None:
        info, videos = await asyncio.to_thread(self.yt.fetch_library)
        self.info = ChannelInfo(
            name=info.name or self.info.name,
            channel_id=info.channel_id or self.info.channel_id,
            logo=info.logo or self.info.logo,
        )
        if videos:
            self.schedule.set_library(videos)
            self._save_library_cache(videos)
            self.schedule.save()
        self.last_library_refresh = time.time()
        self.library_error = None

    async def _library_loop(self) -> None:
        while True:
            delay = self.cfg.library_refresh_interval
            try:
                await self.refresh_library()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Library refresh failed: %s", exc)
                self.library_error = str(exc)
                delay = min(delay, 300)
            await asyncio.sleep(delay)

    # ----------------------------------------------------------------- live
    def live_changed_waiter(self):
        """An awaitable that completes the next time the live state flips."""
        return self._live_event.wait()

    def request_live_check(self) -> None:
        self._check_now.set()

    def set_live(self, live: LiveInfo | None) -> None:
        now = time.time()
        if live and self._unplayable_live.get(live.id, 0) > now:
            live = None
        old_id = self.live.id if self.live else None
        new_id = live.id if live else None
        self.live = live
        if live:
            self.schedule.start_live(live, now)
        elif self.schedule.current_live(now):
            self.schedule.end_live(now)
        if old_id != new_id:
            log.info("Live state changed: %s -> %s", old_id or "offline", new_id or "offline")
            self._live_event.set()
            self._live_event = asyncio.Event()
            self.schedule.save()

    async def _live_loop(self) -> None:
        while True:
            try:
                live = await asyncio.to_thread(self.yt.check_live)
                self.set_live(live)
                self.last_live_check = time.time()
                self.live_check_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Keep the previous state; one failed check shouldn't flip the channel.
                log.warning("Live check failed: %s", exc)
                self.live_check_error = str(exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._check_now.wait(), timeout=self.cfg.live_poll_interval)
                # Don't hammer YouTube if something keeps poking us.
                await asyncio.sleep(LIVE_RECHECK_DELAY)
            self._check_now.clear()

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self.schedule.ensure()
            self.schedule.save()

    # ------------------------------------------------------------- playback
    async def resolve(self, video_id: str, live: bool = False) -> ResolvedMedia:
        key = (video_id, live)
        cached = self._resolved.get(key)
        if cached and time.time() - cached[1] < RESOLVE_TTL:
            return cached[0]
        task = self._resolving.get(key)
        if task is None:
            task = asyncio.create_task(asyncio.to_thread(self.yt.resolve, video_id, live))
            self._resolving[key] = task
            task.add_done_callback(lambda _t: self._resolving.pop(key, None))
        result = await asyncio.shield(task)
        self._resolved[key] = (result, time.time())
        return result

    async def _prefetch(self, video_id: str) -> None:
        try:
            await self.resolve(video_id)
        except Exception as exc:
            log.debug("Prefetch of %s failed: %s", video_id, exc)

    async def next_source(self) -> tuple[Source, str | None]:
        """Decide what the pipeline should play right now."""
        now = time.time()
        self.schedule.ensure(now)

        if self.live:
            live = self.live
            key = f"live:{live.id}"
            try:
                resolved = await self.resolve(live.id, live=True)
            except Exception as exc:
                log.warning("Could not open livestream %s: %s", live.id, exc)
                self.report_failure(key, quick=True)
                return Source("slate", duration=3, label="Connecting to live"), None
            return Source("live", resolved.inputs, label=f"LIVE: {live.title}"), key

        prog = self.schedule.current(now)
        if prog and prog.end - now < 2:
            # Not worth starting ffmpeg for the last second; start the next one a bit early.
            prog = self.schedule.next_after(prog) or prog
        if prog is None or prog.kind != "vod":
            remaining = (prog.end - now) if prog else 30
            return Source("slate", duration=max(1, min(remaining, 60)), label="Off air"), None

        key = f"vod:{prog.video_id}"
        try:
            resolved = await self.resolve(prog.video_id)
        except Exception as exc:
            log.warning("Could not open video %s: %s", prog.video_id, exc)
            self.report_failure(key, quick=True)
            return Source("slate", duration=3, label="Next up"), None

        now = time.time()  # resolving can take a few seconds
        if prog.end - now < 2:
            return Source("slate", duration=max(0.5, prog.end - now), label="Next up"), None
        upcoming = self.schedule.next_after(prog)
        if upcoming and upcoming.kind == "vod" and upcoming.video_id:
            asyncio.create_task(self._prefetch(upcoming.video_id))
        return (
            Source(
                "vod",
                resolved.inputs,
                seek=max(0.0, now - prog.start),
                duration=prog.end - now,
                label=prog.title,
            ),
            key,
        )

    def report_failure(self, key: str, quick: bool) -> None:
        """Called when a source couldn't be opened or ffmpeg died on it."""
        kind, _, video_id = key.partition(":")
        self._resolved.pop((video_id, kind == "live"), None)
        if not quick:
            return  # played for a while: probably an expired URL, just re-resolve
        now = time.time()
        recent = [t for t in self._failures.get(key, []) if now - t < FAILURE_WINDOW] + [now]
        self._failures[key] = recent
        if len(recent) < MAX_QUICK_FAILURES:
            return
        self._failures.pop(key, None)
        if kind == "vod":
            self.schedule.ban(video_id, now)
            self.schedule.save()
        elif kind == "live":
            log.warning("Livestream %s keeps failing; falling back to videos for a while", video_id)
            self._unplayable_live[video_id] = now + UNPLAYABLE_LIVE_COOLDOWN
            self.set_live(None)

    # --------------------------------------------------------------- status
    def status(self) -> dict:
        now = time.time()
        current = self.schedule.current(now)
        upcoming = self.schedule.next_after(current) if current else None
        playing = self.player.now_playing
        return {
            "channel": {
                "name": self.name,
                "url": self.cfg.channel_url,
                "logo": self.logo,
                "tvg_id": self.cfg.tvg_id,
            },
            "live": (
                {"id": self.live.id, "title": self.live.title, "url": f"https://www.youtube.com/watch?v={self.live.id}"}
                if self.live
                else None
            ),
            "now": current.to_dict() if current else None,
            "next": upcoming.to_dict() if upcoming else None,
            "pipeline": {
                "running": self.player.running,
                "viewers": len(self.broadcaster.clients),
                "playing": {"kind": playing.kind, "label": playing.label} if playing else None,
                "bytes_sent": self.broadcaster.bytes_sent,
            },
            "library": {
                "videos": len(self.schedule.library),
                "last_refresh": self.last_library_refresh,
                "error": self.library_error,
            },
            "live_check": {"last": self.last_live_check, "error": self.live_check_error},
        }
