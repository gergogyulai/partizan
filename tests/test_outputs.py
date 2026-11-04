from pathlib import Path
from xml.etree import ElementTree as ET

from partizan.config import Config, normalize_channel_url
from partizan.epg import build_guide_json, build_xmltv
from partizan.schedule import Programme
from partizan.streamer import Source, build_ffmpeg_cmd
from partizan.youtube import MediaInput


def cfg(**kw):
    base = dict(channel="@x", channel_url="https://www.youtube.com/@x", data_dir=Path("/tmp/x"))
    base.update(kw)
    return Config(**base)


def test_normalize_channel_url():
    assert normalize_channel_url("@Foo") == "https://www.youtube.com/@Foo"
    assert (
        normalize_channel_url("UCabcdefghijklmnopqrstuv")
        == "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"
    )
    assert normalize_channel_url("https://www.youtube.com/@Foo/videos/") == "https://www.youtube.com/@Foo"
    assert normalize_channel_url("youtube.com/@Foo/live") == "https://youtube.com/@Foo"


def test_ffmpeg_cmd_vod_two_inputs_seek_and_offset():
    src = Source(
        "vod",
        [MediaInput("https://v.example/video", {"User-Agent": "UA"}), MediaInput("https://v.example/audio")],
        seek=125.5,
        duration=300,
    )
    cmd = build_ffmpeg_cmd(cfg(), src, ts_offset=42.0)
    assert cmd.count("-i") == 2 and cmd.count("-ss") == 2
    assert cmd[cmd.index("-ss") + 1] == "125.500"
    assert cmd[cmd.index("-headers") + 1] == "User-Agent: UA\r\n"
    assert cmd[cmd.index("-output_ts_offset") + 1] == "42.000"
    assert "1:a:0" in cmd and "libx264" in cmd
    assert cmd[-1] == "pipe:1"


def test_ffmpeg_cmd_copy_mode_and_live():
    src = Source("live", [MediaInput("https://live.example/index.m3u8")])
    cmd = build_ffmpeg_cmd(cfg(mode="copy"), src, ts_offset=1)
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "-ss" not in cmd and "-t" not in cmd
    assert "0:a:0?" in cmd


def test_ffmpeg_cmd_custom_video_args():
    src = Source("vod", [MediaInput("/file.mp4")])
    cmd = build_ffmpeg_cmd(cfg(ffmpeg_video_args=["-c:v", "h264_vaapi"]), src, ts_offset=1)
    assert "h264_vaapi" in cmd and "libx264" not in cmd
    assert "-reconnect" not in cmd


def test_xmltv_and_json():
    progs = [
        Programme("vod", 1_800_000_000, 1_800_000_600, "A & B <test>", "abc", "desc"),
        Programme("live", 1_800_000_600, 1_800_004_200, "Live now", "xyz"),
    ]
    xml = build_xmltv("chan", "Channel", "https://logo", progs)
    root = ET.fromstring(xml.split(b"\n", 2)[2])
    assert root.find("channel").get("id") == "chan"
    items = root.findall("programme")
    assert items[0].get("start") == "20270115080000 +0000"
    assert items[0].find("title").text == "A & B <test>"
    assert items[1].find("title").text == "LIVE: Live now"
    assert items[1].find("category").text == "Live"

    data = build_guide_json("chan", "Channel", None, progs)
    assert data["programmes"][1]["kind"] == "live"
    assert data["programmes"][0]["url"] == "https://www.youtube.com/watch?v=abc"
