"""HTTP endpoints: the stream, an M3U playlist, the guide and a small status page."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import time
from datetime import datetime

from aiohttp import web

from .channel import Channel
from .config import Config
from .epg import build_guide_json, build_xmltv

log = logging.getLogger(__name__)

CHANNEL_KEY = web.AppKey("channel", Channel)


def _base_url(request: web.Request) -> str:
    cfg: Config = request.app[CHANNEL_KEY].cfg
    if cfg.base_url:
        return cfg.base_url
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}"


def _guide_window(channel: Channel, request: web.Request):
    now = time.time()
    try:
        hours = float(request.query.get("hours", channel.cfg.epg_hours))
        past = float(request.query.get("past_hours", channel.cfg.epg_past_hours))
    except ValueError:
        raise web.HTTPBadRequest(text="hours and past_hours must be numbers")
    channel.schedule.ensure(now)
    return channel.schedule.window(now - past * 3600, now + hours * 3600)


# ------------------------------------------------------------------- stream
async def stream(request: web.Request) -> web.StreamResponse:
    channel = request.app[CHANNEL_KEY]
    response = web.StreamResponse(
        headers={
            "Content-Type": "video/mp2t",
            "Cache-Control": "no-cache, no-store",
            "Connection": "close",
        }
    )
    await response.prepare(request)
    if request.method == "HEAD":
        return response
    client = channel.broadcaster.subscribe()
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(client.get(), timeout=15)
            except asyncio.TimeoutError:
                # No data for a while; make sure the viewer is still there.
                if request.transport is None or request.transport.is_closing():
                    break
                continue
            if chunk is None:
                break
            await response.write(chunk)
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        channel.broadcaster.unsubscribe(client)
    return response


# ----------------------------------------------------------------- playlist
async def playlist(request: web.Request) -> web.Response:
    channel = request.app[CHANNEL_KEY]
    base = _base_url(request)
    attrs = [
        f'tvg-id="{channel.cfg.tvg_id}"',
        f'tvg-name="{channel.name}"',
        f'tvg-chno="{channel.cfg.channel_number}"',
        'group-title="YouTube"',
    ]
    if channel.logo:
        attrs.append(f'tvg-logo="{channel.logo}"')
    body = (
        f'#EXTM3U url-tvg="{base}/epg.xml" x-tvg-url="{base}/epg.xml"\n'
        f'#EXTINF:-1 {" ".join(attrs)},{channel.name}\n'
        f"{base}/stream.ts\n"
    )
    return web.Response(text=body, content_type="audio/x-mpegurl", charset="utf-8")


# -------------------------------------------------------------------- guide
async def epg_xml(request: web.Request) -> web.Response:
    channel = request.app[CHANNEL_KEY]
    body = build_xmltv(
        channel.cfg.tvg_id, channel.name, channel.logo, _guide_window(channel, request), channel.cfg.channel_url
    )
    return web.Response(body=body, content_type="application/xml", charset="utf-8")


async def guide_json(request: web.Request) -> web.Response:
    channel = request.app[CHANNEL_KEY]
    data = build_guide_json(channel.cfg.tvg_id, channel.name, channel.logo, _guide_window(channel, request))
    return web.json_response(data)


async def status(request: web.Request) -> web.Response:
    return web.json_response(request.app[CHANNEL_KEY].status())


async def refresh(request: web.Request) -> web.Response:
    """Force a live check and a library refresh (e.g. from a webhook or cron)."""
    channel = request.app[CHANNEL_KEY]
    channel.request_live_check()
    asyncio.create_task(_refresh_library(channel))
    return web.json_response({"ok": True}, status=202)


async def _refresh_library(channel: Channel) -> None:
    try:
        await channel.refresh_library()
    except Exception as exc:
        log.warning("Manual library refresh failed: %s", exc)


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# ------------------------------------------------- HDHomeRun (Plex/Jellyfin/Emby)
def _device_id(channel: Channel) -> str:
    return hashlib.sha1(channel.cfg.tvg_id.encode()).hexdigest()[:8].upper()


async def hdhr_discover(request: web.Request) -> web.Response:
    channel = request.app[CHANNEL_KEY]
    base = _base_url(request)
    return web.json_response(
        {
            "FriendlyName": f"partizan ({channel.name})",
            "Manufacturer": "partizan",
            "ModelNumber": "HDTC-2US",
            "FirmwareName": "hdhomeruntc_atsc",
            "FirmwareVersion": "20200101",
            "DeviceID": _device_id(channel),
            "DeviceAuth": "partizan",
            "TunerCount": 4,
            "BaseURL": base,
            "LineupURL": f"{base}/lineup.json",
        }
    )


async def hdhr_lineup(request: web.Request) -> web.Response:
    channel = request.app[CHANNEL_KEY]
    return web.json_response(
        [
            {
                "GuideNumber": channel.cfg.channel_number,
                "GuideName": channel.name,
                "URL": f"{_base_url(request)}/stream.ts",
            }
        ]
    )


async def hdhr_lineup_status(request: web.Request) -> web.Response:
    return web.json_response(
        {"ScanInProgress": 0, "ScanPossible": 1, "Source": "Cable", "SourceList": ["Cable"]}
    )


# --------------------------------------------------------------- status page
def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %H:%M")


async def index(request: web.Request) -> web.Response:
    channel = request.app[CHANNEL_KEY]
    base = _base_url(request)
    now = time.time()
    channel.schedule.ensure(now)
    rows = []
    for prog in channel.schedule.window(now, now + 12 * 3600)[:30]:
        label = "LIVE" if prog.kind == "live" else ("now" if prog.start <= now < prog.end else "")
        title = html.escape(prog.title)
        if prog.video_id:
            title = f'<a href="https://www.youtube.com/watch?v={prog.video_id}">{title}</a>'
        rows.append(
            f"<tr><td>{_fmt(prog.start)}</td><td>{_fmt(prog.end)}</td>"
            f"<td><b>{label}</b></td><td>{title}</td></tr>"
        )
    st = channel.status()
    live = st["live"]
    state = (
        f'<span class="live">LIVE</span> {html.escape(live["title"])}' if live else "Playing videos from the channel"
    )
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>{html.escape(channel.name)}</title>
<style>
:root {{ color-scheme: light dark; --bg:#fafafa; --fg:#1b1b1b; --muted:#666; --line:#ddd; --accent:#c00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141414; --fg:#eee; --muted:#999; --line:#333; --accent:#ff5252; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,sans-serif; margin:0 auto; max-width:900px; padding:16px; }}
h1 {{ font-size:1.4rem; margin:.2rem 0; }} a {{ color:inherit; }}
.muted {{ color:var(--muted); }} .live {{ background:var(--accent); color:#fff; padding:1px 6px; border-radius:4px; font-weight:600; }}
code {{ word-break:break-all; }}
table {{ border-collapse:collapse; width:100%; }} td {{ border-bottom:1px solid var(--line); padding:6px 8px 6px 0; vertical-align:top; }}
td:nth-child(-n+3) {{ white-space:nowrap; color:var(--muted); }}
</style></head><body>
<h1>{html.escape(channel.name)}</h1>
<p>{state}<br><span class="muted">{st["pipeline"]["viewers"]} watching · {st["library"]["videos"]} videos in rotation</span></p>
<p>Playlist: <code>{base}/playlist.m3u</code><br>
Stream: <code>{base}/stream.ts</code><br>
Guide (XMLTV): <code>{base}/epg.xml</code> · <a href="/guide.json">guide.json</a> · <a href="/api/status">status</a></p>
<table>{"".join(rows)}</table>
</body></html>"""
    return web.Response(text=page, content_type="text/html")


# ---------------------------------------------------------------------- app
def create_app(channel: Channel) -> web.Application:
    app = web.Application()
    app[CHANNEL_KEY] = channel

    async def on_startup(app: web.Application) -> None:
        await channel.start()

    async def on_cleanup(app: web.Application) -> None:
        await channel.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    for path in ("/stream.ts", "/stream", "/live.ts", "/auto/{number}"):
        app.router.add_get(path, stream)
    for path in ("/playlist.m3u", "/playlist.m3u8", "/channels.m3u", "/iptv.m3u"):
        app.router.add_get(path, playlist)
    for path in ("/epg.xml", "/xmltv.xml", "/guide.xml"):
        app.router.add_get(path, epg_xml)
    app.router.add_get("/guide.json", guide_json)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/refresh", refresh)
    app.router.add_get("/healthz", health)
    app.router.add_get("/discover.json", hdhr_discover)
    app.router.add_get("/lineup.json", hdhr_lineup)
    app.router.add_get("/lineup_status.json", hdhr_lineup_status)
    app.router.add_get("/", index)
    return app
