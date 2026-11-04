"""Turns the schedule into one continuous MPEG-TS stream.

One ffmpeg process runs per programme (or live stream) and writes MPEG-TS to
stdout. Every process gets ``-output_ts_offset`` set to the time elapsed since
the pipeline started, so timestamps keep increasing across switches and IPTV
players see a single, uninterrupted channel. The bytes are fanned out to all
connected HTTP clients by the :class:`Broadcaster`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from .config import Config
from .youtube import MediaInput

if TYPE_CHECKING:
    from .channel import Channel

log = logging.getLogger(__name__)

TS_PACKET = 188
READ_SIZE = TS_PACKET * 348  # ~64 KiB
CLIENT_QUEUE_CHUNKS = 512  # ~32 MiB of backlog before a client is dropped

FINISHED, INTERRUPTED, FAILED = "finished", "interrupted", "failed"


# --------------------------------------------------------------------- ffmpeg
@dataclass
class Source:
    kind: str  # "vod" | "live" | "slate"
    inputs: list[MediaInput] = field(default_factory=list)
    seek: float = 0.0
    duration: float | None = None
    label: str = ""


def _headers_arg(headers: dict[str, str]) -> list[str]:
    if not headers:
        return []
    return ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]


def build_ffmpeg_cmd(cfg: Config, source: Source, ts_offset: float) -> list[str]:
    cmd = [cfg.ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "warning"]
    w, h, fps = cfg.width, cfg.height, cfg.fps
    transcode_video = cfg.mode == "transcode" or source.kind == "slate"

    if source.kind == "slate":
        cmd += ["-re", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}"]
        cmd += ["-re", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        maps = ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        for media in source.inputs:
            cmd += ["-re"]
            if media.url.startswith("http"):
                cmd += [
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_on_network_error", "1",
                    "-reconnect_delay_max", "5",
                    "-rw_timeout", "15000000",
                ]
                cmd += _headers_arg(media.headers)
            if source.seek >= 1:
                cmd += ["-ss", f"{source.seek:.3f}"]
            cmd += ["-i", media.url]
        if len(source.inputs) >= 2:
            maps = ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            maps = ["-map", "0:v:0", "-map", "0:a:0?"]

    cmd += maps
    if source.duration:
        cmd += ["-t", f"{source.duration:.3f}"]

    if not transcode_video:
        cmd += ["-c:v", "copy"]
    elif cfg.ffmpeg_video_args and source.kind != "slate":
        cmd += cfg.ffmpeg_video_args
    else:
        cmd += [
            "-vf",
            f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p",
            "-c:v", "libx264",
            "-preset", cfg.x264_preset,
            "-profile:v", "high",
            "-b:v", cfg.video_bitrate,
            "-maxrate", cfg.video_bitrate,
            "-bufsize", _double_bitrate(cfg.video_bitrate),
            "-g", str(fps * 2),
            "-keyint_min", str(fps * 2),
            "-sc_threshold", "0",
        ]
    cmd += ["-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "48000", "-ac", "2"]
    cmd += [
        "-output_ts_offset", f"{ts_offset:.3f}",
        "-f", "mpegts",
        "-mpegts_service_id", "1",
        "-metadata", f"service_name={cfg.channel_name or cfg.tvg_id}",
        "-metadata", "service_provider=partizan",
        "-pat_period", "0.5",
        "pipe:1",
    ]
    return cmd


def _double_bitrate(rate: str) -> str:
    unit = rate[-1] if rate[-1].isalpha() else ""
    number = float(rate[:-1] if unit else rate)
    value = number * 2
    return f"{int(value) if value.is_integer() else value}{unit}"


# ---------------------------------------------------------------- broadcaster
class Client:
    def __init__(self):
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=CLIENT_QUEUE_CHUNKS)
        self.closed = False

    async def get(self) -> bytes | None:
        return await self.queue.get()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)


class Broadcaster:
    """Fans MPEG-TS chunks out to every connected client."""

    def __init__(
        self,
        on_first: Callable[[], None] | None = None,
        on_last: Callable[[], None] | None = None,
    ):
        self.clients: set[Client] = set()
        self.on_first = on_first
        self.on_last = on_last
        self.bytes_sent = 0

    def subscribe(self) -> Client:
        client = Client()
        self.clients.add(client)
        log.info("Viewer connected (%d watching)", len(self.clients))
        if len(self.clients) == 1 and self.on_first:
            self.on_first()
        return client

    def unsubscribe(self, client: Client) -> None:
        if client not in self.clients:
            return
        self.clients.discard(client)
        client.close()
        log.info("Viewer disconnected (%d watching)", len(self.clients))
        if not self.clients and self.on_last:
            self.on_last()

    def publish(self, chunk: bytes) -> None:
        self.bytes_sent += len(chunk)
        for client in list(self.clients):
            try:
                client.queue.put_nowait(chunk)
            except asyncio.QueueFull:
                log.warning("Dropping a viewer that can't keep up")
                self.unsubscribe(client)

    def close_all(self) -> None:
        for client in list(self.clients):
            client.close()
        self.clients.clear()


# --------------------------------------------------------------------- player
class Player:
    """Runs the ffmpeg pipeline while somebody is watching (or always, if configured)."""

    def __init__(self, cfg: Config, channel: "Channel", broadcaster: Broadcaster):
        self.cfg = cfg
        self.channel = channel
        self.broadcaster = broadcaster
        self._task: asyncio.Task | None = None
        self._stop_timer: asyncio.TimerHandle | None = None
        self._t0 = 0.0
        self.now_playing: Source | None = None
        self.started_at: float | None = None
        self.recent_stderr: deque[str] = deque(maxlen=20)
        self.history: deque[tuple[float, str, str]] = deque(maxlen=50)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def ensure_running(self) -> None:
        if self._stop_timer:
            self._stop_timer.cancel()
            self._stop_timer = None
        if not self.running:
            log.info("Starting stream pipeline")
            self._task = asyncio.create_task(self._run())

    def stop_later(self) -> None:
        if self.cfg.always_on or self._stop_timer:
            return
        loop = asyncio.get_running_loop()
        self._stop_timer = loop.call_later(
            self.cfg.idle_timeout, lambda: asyncio.create_task(self.stop())
        )

    async def stop(self) -> None:
        self._stop_timer = None
        if self.broadcaster.clients and not self.cfg.always_on:
            return  # somebody tuned back in during the grace period
        if self._task:
            log.info("Stopping stream pipeline (no viewers)")
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            self.now_playing = None

    def ts_offset(self) -> float:
        # One second of margin so a new process never starts behind the previous one.
        return time.monotonic() - self._t0 + 1.0

    async def _run(self) -> None:
        self._t0 = time.monotonic()
        while True:
            try:
                source, key = await self.channel.next_source()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not prepare the next source")
                source, key = Source("slate", duration=10, label="Technical difficulties"), None

            started = time.monotonic()
            result = await self.play(source, interrupt=self.channel.live_changed_waiter())
            ran_for = time.monotonic() - started
            if result == INTERRUPTED:
                continue
            if source.kind == "live":
                # The live ffmpeg only exits on its own when the stream ended or broke,
                # so ask for a fresh live check right away.
                self.channel.request_live_check()
                if result == FINISHED:
                    await self.play(
                        Source("slate", duration=10, label="Checking live status"),
                        interrupt=self.channel.live_changed_waiter(),
                    )
                    continue
            if key and result == FAILED:
                self.channel.report_failure(key, quick=ran_for < 30)
                await self.play(Source("slate", duration=3, label="Reconnecting"))

    async def play(self, source: Source, interrupt: Awaitable | None = None) -> str:
        """Play one source to the broadcaster; returns FINISHED, INTERRUPTED or FAILED."""
        cmd = build_ffmpeg_cmd(self.cfg, source, self.ts_offset())
        log.info(
            "Playing %s: %s (seek %.0fs, duration %s)",
            source.kind,
            source.label,
            source.seek,
            f"{source.duration:.0f}s" if source.duration else "open",
        )
        log.debug("ffmpeg command: %s", cmd)
        self.recent_stderr.clear()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.now_playing = source
        self.started_at = time.time()
        self.history.append((self.started_at, source.kind, source.label))
        pump = asyncio.create_task(self._pump(proc))
        errs = asyncio.create_task(self._drain_stderr(proc))
        waiters = {pump}
        interrupt_task = None
        if interrupt is not None:
            interrupt_task = asyncio.ensure_future(interrupt)
            waiters.add(interrupt_task)
        try:
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if interrupt_task in done:
                log.info("Switching source")
                return INTERRUPTED
        finally:
            if interrupt_task:
                interrupt_task.cancel()
            await _terminate(proc)
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            with contextlib.suppress(asyncio.CancelledError):
                await errs
        if proc.returncode != 0:
            log.warning(
                "ffmpeg exited with %s for %s: %s",
                proc.returncode,
                source.label,
                " | ".join(list(self.recent_stderr)[-3:]),
            )
            return FAILED
        return FINISHED

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        remainder = b""
        while True:
            data = await proc.stdout.read(READ_SIZE)
            if not data:
                break
            data = remainder + data
            cut = len(data) - len(data) % TS_PACKET
            remainder = data[cut:]
            if cut:
                self.broadcaster.publish(data[:cut])
        await proc.wait()

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").strip()
            if text:
                self.recent_stderr.append(text)
                log.debug("ffmpeg: %s", text)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
