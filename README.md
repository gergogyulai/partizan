# partizan

Turn a YouTube channel into a 24/7 IPTV channel on your home server.

- When the channel is **live**, the stream shows the livestream.
- When it isn't, it plays the channel's **uploaded videos** on a schedule, like a TV channel.
- The output is a single continuous **MPEG-TS stream** plus an **M3U playlist**, which IPTV apps (TiviMate, Kodi, VLC, Jellyfin, Emby, Plex…) can play.
- An **XMLTV guide** (`/epg.xml`) and a **JSON guide** (`/guide.json`) list what plays when.

## Quick start (Docker)

```bash
git clone https://github.com/gergogyulai/partizan.git
cd partizan
cp .env.example .env      # then set CHANNEL=... in .env
docker compose up -d --build
```

Open `http://<server-ip>:8080/` for the status page. In your IPTV app, add:

| What | URL |
|---|---|
| Playlist (M3U) | `http://<server-ip>:8080/playlist.m3u` |
| Guide (XMLTV / EPG) | `http://<server-ip>:8080/epg.xml` |

Most apps read the guide URL from the playlist automatically (`url-tvg`).

### Without Docker

Requires Python 3.11+ and ffmpeg.

```bash
pip install -r requirements.txt
```

```bash
CHANNEL=@YourChannel python -m partizan
```

## Endpoints

| Path | Description |
|---|---|
| `/stream.ts` | The channel as a continuous MPEG-TS stream (aliases: `/stream`, `/live.ts`) |
| `/playlist.m3u` | M3U playlist with the channel, logo and guide link |
| `/epg.xml` | XMLTV guide. `?hours=` and `?past_hours=` change the window |
| `/guide.json` | The same guide as JSON, with video links and thumbnails |
| `/api/status` | What's on now/next, live state, viewer count, last errors |
| `POST /api/refresh` | Check for a livestream now and reload the video list |
| `/discover.json`, `/lineup.json` | HDHomeRun emulation for Plex / Jellyfin / Emby |
| `/healthz` | Health check |
| `/` | Status page with the next 12 hours |

### App notes

- **TiviMate / IPTV Smarters / OTT Navigator / Kodi PVR IPTV Simple**: add the playlist URL. If the guide isn't picked up, add the EPG URL by hand.
- **VLC**: open the playlist or `/stream.ts` as a network stream.
- **Jellyfin / Emby**: Live TV → add an *M3U tuner* with the playlist URL and an *XMLTV* guide provider with the EPG URL. You can also add it as an HDHomeRun tuner at `http://<server-ip>:8080`.
- **Plex**: Live TV & DVR → set up a device → enter `http://<server-ip>:8080` by hand (it emulates an HDHomeRun), then use the XMLTV URL as the guide.

## How it works

```
                ┌───────────── yt-dlp ─────────────┐
                │ video list (every 6h)             │
 YouTube ───────┤ live check (every 60s)            ├──► schedule ──► guide (XMLTV / JSON)
                │ stream URLs (just before playing) │        │
                └───────────────────────────────────┘        ▼
                                          ffmpeg (one per programme) ──► MPEG-TS ──► all viewers
```

- **Schedule.** Videos are planned back-to-back 48 hours ahead (`EPG_HOURS`), so the guide is accurate. The plan is saved in `/data`, so a restart doesn't reshuffle it. Order is `shuffle` (every video plays once before any repeats), `newest` or `oldest`. Newly uploaded videos play straight after the current programme.
- **It behaves like TV.** Programmes follow the clock whether or not anyone is watching. When you tune in, playback starts at the right point in the scheduled video. ffmpeg only runs while someone is watching (or always, with `ALWAYS_ON=true`). Everyone watching shares one ffmpeg process.
- **Live.** The channel's `/live` page is checked every `LIVE_POLL_INTERVAL` seconds. When a livestream starts, the current video is cut, the stream switches to the live, and the guide shows a `LIVE:` block. That block is assumed to last `LIVE_BLOCK_MINUTES` and keeps extending while the stream runs. When the live ends, scheduled videos resume and the guide is rebuilt from that point.
- **One continuous stream.** Each source runs in its own ffmpeg, but they all use the same output format and their timestamps carry on from one to the next. To the player, the switches look like one uninterrupted channel.
- **Failures.** If a video can't be opened (deleted, private, region-locked…), it is skipped and the plan is rebuilt. Expired URLs are fetched again and playback resumes at the same point. If a livestream can't be played (e.g. members-only), the channel stays on videos.

## Configuration

Everything is set with environment variables. See [`.env.example`](.env.example) for the full list.

| Variable | Default | |
|---|---|---|
| `CHANNEL` | — | **Required.** `@handle`, channel URL or `UC…` id |
| `CHANNEL_NAME`, `CHANNEL_LOGO` | from YouTube | Override the name / logo |
| `BASE_URL` | from request | Public URL used inside the playlist (set it if you use a reverse proxy) |
| `LIBRARY_TABS` | `videos` | Comma-separated tabs to take videos from, e.g. `videos,streams` to include past livestreams |
| `LIBRARY_MAX` | `300` | Max videos per tab |
| `MIN_DURATION` / `MAX_DURATION` | `60` / `0` | Filter videos by length, in seconds |
| `ORDER` | `shuffle` | `shuffle`, `newest` or `oldest` |
| `LIVE_POLL_INTERVAL` | `60` | Seconds between live checks |
| `LIVE_BLOCK_MINUTES` | `60` | Guide length of a livestream (extended while live) |
| `EPG_HOURS` / `EPG_PAST_HOURS` | `48` / `6` | Guide window |
| `MODE` | `transcode` | `transcode` (re-encode to fixed H.264, most compatible) or `copy` (no video re-encode, very low CPU) |
| `VIDEO_HEIGHT`, `FPS`, `VIDEO_BITRATE`, `X264_PRESET` | `720`, `30`, `3000k`, `veryfast` | Output format in `transcode` mode |
| `FFMPEG_VIDEO_ARGS` | — | Replace the video encoder arguments, e.g. for hardware encoding |
| `ALWAYS_ON` | `false` | Keep streaming with no viewers (instant tune-in, constant CPU) |
| `IDLE_TIMEOUT` | `30` | Seconds to keep streaming after the last viewer leaves |
| `YTDLP_COOKIES` | — | Path to a cookies.txt if YouTube asks you to sign in |
| `UPDATE_YTDLP` | `true` | Docker only: update yt-dlp when the container starts |

### CPU and modes

`transcode` re-encodes everything to 720p30 H.264. That costs a modern x86 core or two. `MODE=copy` passes YouTube's H.264 through untouched and uses almost no CPU, but the resolution can change between videos. Most players handle that; some Android players stutter briefly at the switch.

## Notes and limits

- **yt-dlp has to stay current.** YouTube changes often. The Docker image updates yt-dlp on every start; rebuild or restart if playback stops working. yt-dlp needs a JavaScript runtime for YouTube (Deno, installed via the `deno` extra).
- **Live detection is polled.** A livestream appears within about `LIVE_POLL_INTERVAL` seconds of starting. Its end time in the guide is an estimate.
- **Tune-in takes a few seconds.** Unless `ALWAYS_ON=true`, the first viewer waits while stream URLs are fetched and ffmpeg starts.
- **Home use.** This is meant for watching a channel on your own devices. Respect YouTube's terms and the creator's rights, and don't expose the stream publicly.

## Development

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

The end-to-end test runs the real server and ffmpeg against a fake YouTube backend serving local clips. It checks that the stream switches from videos to a live and back without timestamp jumps.
