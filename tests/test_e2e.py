"""End-to-end: real ffmpeg + HTTP server, with a fake YouTube backend serving local files."""

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from partizan import channel as channel_mod
from partizan.channel import Channel
from partizan.config import Config
from partizan.server import create_app
from partizan.youtube import ChannelInfo, LiveInfo, MediaInput, ResolvedMedia, Video

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def make_clip(path: Path, seconds: int, color: str, size="640x360") -> Path:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=s={size}:r=25:d={seconds}",
            "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
            "-vf", f"drawbox=c={color}:t=40", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )
    return path


class FakeYouTube:
    def __init__(self, files):
        self.files = files
        self.live = None
        self.live_checks = 0

    def fetch_library(self):
        return ChannelInfo(name="Test Channel"), [
            Video("a", "Video A", 6, index=0),
            Video("b", "Video B", 6, index=1),
        ]

    def check_live(self):
        self.live_checks += 1
        return self.live

    def resolve(self, video_id, live=False):
        return ResolvedMedia([MediaInput(str(self.files[video_id]))], is_live=live)


def probe_packets(path: Path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "packet=stream_index,pts_time,codec_type",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["packets"]


def test_stream_switches_between_vod_and_live(tmp_path, monkeypatch):
    monkeypatch.setattr(channel_mod, "LIVE_RECHECK_DELAY", 0.2)
    files = {
        "a": make_clip(tmp_path / "a.mp4", 6, "red"),
        "b": make_clip(tmp_path / "b.mp4", 6, "blue", size="1280x720"),
        "live": make_clip(tmp_path / "live.mp4", 60, "green"),
    }
    fake = FakeYouTube(files)
    cfg = Config(
        channel="@test", channel_url="https://www.youtube.com/@test", tvg_id="test",
        data_dir=tmp_path / "data", width=320, height=180, fps=15, x264_preset="ultrafast",
        video_bitrate="300k", live_poll_interval=2, idle_timeout=1, epg_hours=1, live_block=600,
    )
    channel = Channel(cfg, yt=fake)
    out = tmp_path / "out.ts"
    timeline = {}

    async def scenario():
        server = TestServer(create_app(channel))
        await server.start_server()
        base = str(server.make_url(""))
        try:
            async with aiohttp.ClientSession() as http:
                for _ in range(50):  # library loads in the background
                    async with http.get(f"{base}/api/status") as r:
                        if (await r.json())["library"]["videos"]:
                            break
                    await asyncio.sleep(0.1)
                async with http.get(f"{base}/playlist.m3u") as r:
                    m3u = await r.text()
                    assert "#EXTM3U" in m3u and "/stream.ts" in m3u and 'tvg-id="test"' in m3u
                async with http.get(f"{base}/epg.xml") as r:
                    assert r.status == 200 and b"<programme" in await r.read()
                async with http.get(f"{base}/guide.json") as r:
                    guide = await r.json()
                    on_now = [p for p in guide["programmes"] if p["now_playing"]]
                    assert on_now[0]["kind"] == "vod"
                async with http.get(f"{base}/lineup.json") as r:
                    assert (await r.json())[0]["URL"].endswith("/stream.ts")

                start = time.monotonic()
                with out.open("wb") as fh:
                    async with http.get(f"{base}/stream.ts") as resp:
                        assert resp.headers["Content-Type"] == "video/mp2t"
                        async for chunk in resp.content.iter_chunked(65536):
                            fh.write(chunk)
                            t = time.monotonic() - start
                            if t > 9 and "live_on" not in timeline:
                                timeline["live_on"] = t
                                fake.live = LiveInfo("live", "Breaking news")
                                channel.request_live_check()
                            if t > 13 and "status_live" not in timeline:
                                async with http.get(f"{base}/api/status") as r:
                                    timeline["status_live"] = await r.json()
                                async with http.get(f"{base}/guide.json") as r:
                                    timeline["guide_live"] = await r.json()
                            if t > 17 and "live_off" not in timeline:
                                timeline["live_off"] = t
                                fake.live = None
                                channel.request_live_check()
                            if t > 26:
                                break
                assert channel.player.running
                await asyncio.sleep(2.5)  # idle timeout is 1s
                assert not channel.player.running, "pipeline should stop without viewers"
        finally:
            await server.close()

    asyncio.run(scenario())

    played = [(kind, label) for _, kind, label in channel.player.history]
    kinds = [k for k, _ in played]
    assert "live" in kinds
    after_live = played[kinds.index("live") + 1]
    assert after_live[0] == "vod", f"expected videos to resume right after the live, got {played}"
    assert all(label != "Next up" for _, label in played), played

    status = timeline["status_live"]
    assert status["live"]["id"] == "live"
    assert status["pipeline"]["playing"]["kind"] == "live"
    assert status["now"]["kind"] == "live"
    live_progs = [p for p in timeline["guide_live"]["programmes"] if p["kind"] == "live"]
    assert live_progs and live_progs[0]["now_playing"]

    # The captured bytes must be one valid, continuous MPEG-TS stream.
    packets = probe_packets(out)
    video = [float(p["pts_time"]) for p in packets if p["codec_type"] == "video" and "pts_time" in p]
    audio = [float(p["pts_time"]) for p in packets if p["codec_type"] == "audio" and "pts_time" in p]
    assert video and audio
    assert video[-1] - video[0] > 18, "expected ~20s+ of video"
    # PTS can reorder within a GOP (B-frames), but must never jump back across a switch.
    running_max = video[0]
    for pts in video:
        assert pts > running_max - 1.0, "timestamps went backwards across a source switch"
        running_max = max(running_max, pts)
    decode = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"], capture_output=True, text=True
    )
    assert decode.returncode == 0
