"""Guide output: XMLTV for IPTV apps, JSON for everything else."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from .schedule import Programme


def _xmltv_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _description(prog: Programme, limit: int = 1000) -> str:
    text = prog.description or ""
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    if prog.video_id:
        text = (text + "\n\n" if text else "") + f"https://www.youtube.com/watch?v={prog.video_id}"
    return text


def build_xmltv(
    tvg_id: str,
    name: str,
    logo: str | None,
    programmes: list[Programme],
    site_url: str | None = None,
) -> bytes:
    tv = ET.Element("tv", {"generator-info-name": "partizan"})
    channel = ET.SubElement(tv, "channel", {"id": tvg_id})
    ET.SubElement(channel, "display-name").text = name
    if logo:
        ET.SubElement(channel, "icon", {"src": logo})
    if site_url:
        ET.SubElement(channel, "url").text = site_url

    for prog in programmes:
        el = ET.SubElement(
            tv,
            "programme",
            {"start": _xmltv_time(prog.start), "stop": _xmltv_time(prog.end), "channel": tvg_id},
        )
        title = f"LIVE: {prog.title}" if prog.kind == "live" else prog.title
        ET.SubElement(el, "title").text = title
        desc = _description(prog)
        if desc:
            ET.SubElement(el, "desc").text = desc
        if prog.kind == "live":
            ET.SubElement(el, "category").text = "Live"
        elif prog.kind == "vod":
            ET.SubElement(el, "category").text = "Video"
        if prog.thumbnail:
            ET.SubElement(el, "icon", {"src": prog.thumbnail})

    ET.indent(tv)
    body = ET.tostring(tv, encoding="unicode").encode("utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n' + body + b"\n"


def build_guide_json(tvg_id: str, name: str, logo: str | None, programmes: list[Programme]) -> dict:
    now = time.time()
    return {
        "channel": {"id": tvg_id, "name": name, "logo": logo},
        "generated_at": _iso(now),
        "programmes": [
            {
                "kind": p.kind,
                "title": p.title,
                "start": _iso(p.start),
                "end": _iso(p.end),
                "start_ts": int(p.start),
                "end_ts": int(p.end),
                "duration": int(p.duration),
                "video_id": p.video_id,
                "url": f"https://www.youtube.com/watch?v={p.video_id}" if p.video_id else None,
                "thumbnail": p.thumbnail,
                "description": p.description,
                "now_playing": p.start <= now < p.end,
                # A live programme's end is an estimate that moves while the stream runs.
                "end_is_estimate": p.kind == "live" and p.end > now,
            }
            for p in programmes
        ],
    }
