"""The channel timeline: what plays when.

The schedule is a list of back-to-back programmes on a wall-clock timeline.
VOD programmes are planned ahead (``horizon`` seconds) so a guide can be served;
a live programme is inserted when the channel goes live and the plan after it
is shifted while the stream keeps running.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .youtube import LiveInfo, Video, thumbnail_url

log = logging.getLogger(__name__)

OFFAIR_BLOCK = 30 * 60


@dataclass
class Programme:
    kind: str  # "vod" | "live" | "offair"
    start: float
    end: float
    title: str
    video_id: str | None = None
    description: str = ""

    @property
    def thumbnail(self) -> str | None:
        return thumbnail_url(self.video_id) if self.video_id else None

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return asdict(self)


class Schedule:
    def __init__(
        self,
        order: str = "shuffle",
        horizon: float = 48 * 3600,
        history: float = 6 * 3600,
        live_block: float = 3600,
        path: Path | None = None,
        rng: random.Random | None = None,
    ):
        self.order = order
        self.horizon = horizon
        self.history = history
        self.live_block = live_block
        self.path = path
        self.rng = rng or random.Random()
        self.library: dict[str, Video] = {}
        self.banned: set[str] = set()
        self.queue: list[str] = []  # upcoming video ids, consumed front first
        self.items: list[Programme] = []
        self.last_played: str | None = None

    # ---------------------------------------------------------------- library
    def set_library(self, videos: list[Video], now: float | None = None) -> None:
        now = time.time() if now is None else now
        old_ids = set(self.library)
        self.library = {v.id: v for v in videos}
        self.banned.clear()  # give previously failing videos another chance
        new_ids = [v.id for v in sorted(videos, key=lambda v: v.index) if v.id not in old_ids]
        if old_ids and new_ids:
            # Freshly uploaded videos air right after whatever is on now.
            log.info("%d new video(s) in the library, scheduling them next", len(new_ids))
            current = self.current(now)
            self._cut_at(current.end if current else now)
            self.queue = new_ids + [i for i in self.queue if i not in new_ids]
            self.queue = [i for i in self.queue if i in self.library]
            self.ensure(now)
            return
        self.queue = [i for i in self.queue if i in self.library]
        # Replace placeholder blocks (no library yet) with real content.
        if any(p.kind == "offair" and p.end > now for p in self.items) and self._playable_ids():
            self.rebuild_from(now)
        else:
            self.ensure(now)

    def ban(self, video_id: str, now: float | None = None) -> None:
        """Drop a video that can't be played and replan from now."""
        now = time.time() if now is None else now
        log.warning("Skipping unplayable video %s", video_id)
        self.banned.add(video_id)
        self.rebuild_from(now)

    def _playable_ids(self) -> list[str]:
        return [i for i in self.library if i not in self.banned]

    def _refill_queue(self) -> None:
        ids = self._playable_ids()
        if not ids:
            return
        if self.order == "newest":
            ids.sort(key=lambda i: self.library[i].index)
        elif self.order == "oldest":
            ids.sort(key=lambda i: self.library[i].index, reverse=True)
        else:
            self.rng.shuffle(ids)
            if len(ids) > 1 and ids[0] == self.last_played:
                ids.append(ids.pop(0))
        self.queue.extend(ids)

    def _next_video(self) -> Video | None:
        for _ in range(2):
            while self.queue:
                vid = self.queue.pop(0)
                if vid in self.library and vid not in self.banned:
                    self.last_played = vid
                    return self.library[vid]
            self._refill_queue()
        return None

    # --------------------------------------------------------------- timeline
    def ensure(self, now: float | None = None) -> None:
        """Drop old history and plan ahead up to the horizon."""
        now = time.time() if now is None else now
        self.items = [p for p in self.items if p.end > now - self.history]
        if not self.items or self.items[-1].end < now:
            # Nothing covers "now" (first start, or the server was off for a while).
            cursor = now
        else:
            cursor = self.items[-1].end
        while cursor < now + self.horizon:
            video = self._next_video()
            if video is None:
                prog = Programme("offair", cursor, cursor + OFFAIR_BLOCK, "Off air")
            else:
                prog = Programme(
                    "vod", cursor, cursor + video.duration, video.title, video.id, video.description
                )
            self.items.append(prog)
            cursor = prog.end

    def _push_back(self, programmes: list[Programme]) -> None:
        """Return planned-but-unplayed videos to the front of the queue."""
        ids = [p.video_id for p in programmes if p.kind == "vod" and p.video_id]
        if ids:
            self.queue = ids + [i for i in self.queue if i not in ids]

    def _cut_at(self, t: float) -> None:
        """Throw away the plan from ``t`` on, cutting short a programme that spans it."""
        self._push_back([p for p in self.items if p.start >= t])
        self.items = [p for p in self.items if p.start < t]
        if self.items and self.items[-1].end > t:
            self.items[-1].end = t

    def rebuild_from(self, t: float, now: float | None = None) -> None:
        self._cut_at(t)
        self.ensure(t if now is None else now)

    def current(self, now: float | None = None) -> Programme | None:
        now = time.time() if now is None else now
        for prog in self.items:
            if prog.start <= now < prog.end:
                return prog
        return None

    def next_after(self, prog: Programme) -> Programme | None:
        for candidate in self.items:
            if candidate.start >= prog.end - 0.001 and candidate is not prog:
                return candidate
        return None

    def window(self, start: float, end: float) -> list[Programme]:
        return [p for p in self.items if p.end > start and p.start < end]

    # ------------------------------------------------------------------- live
    def current_live(self, now: float | None = None) -> Programme | None:
        prog = self.current(now)
        return prog if prog and prog.kind == "live" else None

    def start_live(self, live: LiveInfo, now: float | None = None) -> None:
        now = time.time() if now is None else now
        current = self.current_live(now)
        if current and current.video_id == live.id:
            self.update_live(live, now)
            return
        if current:
            self.end_live(now)
        log.info("Live: %s (%s)", live.title, live.id)
        self._cut_at(now)
        self.items.append(
            Programme("live", now, now + self.live_block, live.title, live.id, live.description)
        )
        self.ensure(now)

    def update_live(self, live: LiveInfo, now: float | None = None) -> None:
        """Keep the live block in the guide ahead of the current time."""
        now = time.time() if now is None else now
        prog = self.current_live(now)
        if not prog:
            return
        prog.title = live.title or prog.title
        prog.description = live.description or prog.description
        if prog.end - now < self.live_block / 2:
            self._cut_at(prog.end)
            prog.end = now + self.live_block
            self.ensure(now)

    def end_live(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        prog = self.current_live(now)
        if not prog:
            return
        log.info("Live ended: %s", prog.title)
        self.rebuild_from(now, now)

    # ------------------------------------------------------------ persistence
    def save(self) -> None:
        if not self.path:
            return
        data = {
            "items": [p.to_dict() for p in self.items],
            "queue": self.queue,
            "last_played": self.last_played,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            self.items = [Programme(**p) for p in data.get("items", [])]
            self.queue = list(data.get("queue", []))
            self.last_played = data.get("last_played")
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("Ignoring unreadable schedule file %s: %s", self.path, exc)
            self.items, self.queue = [], []
