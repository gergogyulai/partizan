<div align="center">
  <br>
  <h1>partizan</h1>
  <sub>A YouTube channel as a 24/7 IPTV channel, on your home server</sub>
  <br>
  <br>
</div>

> **Early.** Tested end to end against local clips with a fake YouTube backend, but not yet run against YouTube itself.

partizan turns one YouTube channel into a TV channel that IPTV apps can play. When the channel is live, it shows the livestream. When it isn't, it plays the channel's uploads on a schedule and serves a guide of what plays when.

- 🔴 Switches to the livestream within a minute of it starting, and back to videos when it ends
- 📼 Plays uploads back to back on a 48-hour schedule: shuffled, newest first or oldest first
- 🆕 New uploads play straight after whatever is on now
- 📺 One continuous MPEG-TS stream plus an M3U playlist for TiviMate, Kodi, VLC, Jellyfin, Emby or Plex
- 🗓️ XMLTV guide (`/epg.xml`) and a JSON guide (`/guide.json`) with video links and thumbnails
- ⏱️ Runs on the clock like real TV. Tuning in starts mid-video, where the guide says.
- 💤 ffmpeg only runs while someone is watching, and all viewers share one stream
- 📡 Pretends to be an HDHomeRun tuner, for Plex, Jellyfin and Emby

## Install

You need Docker.

```sh
git clone https://github.com/gergogyulai/partizan.git
cd partizan
cp .env.example .env          # set CHANNEL=@yourchannel
docker compose up -d --build
```

`CHANNEL` takes an `@handle`, a channel URL or a `UC…` channel id. The status page is at `http://<server-ip>:8080/`.

<details>
<summary>Without Docker</summary>

You need Python 3.11 or later and ffmpeg.

```sh
pip install -r requirements.txt
CHANNEL=@yourchannel python -m partizan
```

Keep yt-dlp updated yourself (`pip install -U "yt-dlp[default,deno]"`). The Docker image does it on every start.
</details>

### Adding it to an app

| App | How |
|---|---|
| TiviMate, OTT Navigator, IPTV Smarters, Kodi PVR IPTV Simple | Add `http://<server-ip>:8080/playlist.m3u`. The guide is linked from the playlist; if it isn't picked up, add `/epg.xml` by hand. |
| VLC | Open the playlist, or `/stream.ts` as a network stream |
| Jellyfin, Emby | Live TV → M3U tuner with the playlist URL, XMLTV guide with `/epg.xml`. Or add it as an HDHomeRun tuner at `http://<server-ip>:8080`. |
| Plex | Live TV & DVR → set up a device → enter `http://<server-ip>:8080` by hand, then use `/epg.xml` as the guide |

## Endpoints

| Path | What |
|---|---|
| `/stream.ts` | The channel (also `/stream`, `/live.ts`) |
| `/playlist.m3u` | M3U playlist with the channel, logo and guide link |
| `/epg.xml` | XMLTV guide. `?hours=` and `?past_hours=` change the window. |
| `/guide.json` | The same guide as JSON |
| `/api/status` | What's on now and next, live state, viewers, last errors |
| `POST /api/refresh` | Check for a livestream and reload the video list now |
| `/discover.json`, `/lineup.json` | HDHomeRun emulation |
| `/` | Status page with the next 12 hours |

## Configuration

Everything is an environment variable in `.env`. [`.env.example`](.env.example) lists them all.

| Variable | Default | What |
|---|---|---|
| `CHANNEL` | | **Required.** The channel to follow |
| `CHANNEL_NAME`, `CHANNEL_LOGO` | from YouTube | Override the name and logo |
| `BASE_URL` | from the request | Address used inside the playlist, for reverse proxies |
| `LIBRARY_TABS` | `videos` | Tabs to take videos from. `videos,streams` adds past livestreams. |
| `MIN_DURATION`, `MAX_DURATION` | `60`, `0` | Skip videos shorter or longer than this, in seconds (`0` = no limit) |
| `ORDER` | `shuffle` | `shuffle`, `newest` or `oldest` |
| `LIVE_POLL_INTERVAL` | `60` | Seconds between live checks |
| `LIVE_BLOCK_MINUTES` | `60` | How long a livestream is assumed to run in the guide. It keeps extending while the stream is live. |
| `EPG_HOURS`, `EPG_PAST_HOURS` | `48`, `6` | Guide window |
| `MODE` | `transcode` | `transcode` re-encodes everything to one H.264 format. `copy` passes YouTube's H.264 through. |
| `VIDEO_HEIGHT`, `FPS`, `VIDEO_BITRATE`, `X264_PRESET` | `720`, `30`, `3000k`, `veryfast` | Output in `transcode` mode |
| `FFMPEG_VIDEO_ARGS` | | Replaces the video encoder arguments, e.g. for hardware encoding |
| `ALWAYS_ON` | `false` | Keep streaming with nobody watching, for instant tune-in |
| `IDLE_TIMEOUT` | `30` | Seconds to keep streaming after the last viewer leaves |
| `YTDLP_COOKIES` | | Path to a cookies.txt, if YouTube asks you to sign in |
| `UPDATE_YTDLP` | `true` | Update yt-dlp when the container starts |

`transcode` costs about one or two CPU cores at 720p30. `copy` uses almost none, but the resolution can change between videos, and some Android players stutter briefly when it does.

## How it works

| Piece | How |
|---|---|
| video list | yt-dlp lists the channel's tabs every 6 hours, without opening each video. It's cached in `/data`. |
| schedule | Videos are planned back to back 48 hours ahead and saved in `/data`, so a restart doesn't reshuffle them. Shuffle plays every video once before any repeats. |
| live detection | yt-dlp checks the channel's `/live` page every minute. A live cuts the current video and gets a `LIVE:` block in the guide; when it ends, the schedule is rebuilt from that point. |
| playback | One ffmpeg per programme, seeking to where the schedule says it should be, and stopping at the scheduled end so the stream never drifts from the guide |
| one stream | Each ffmpeg writes MPEG-TS with its timestamps offset to carry on from the last one, so players see one uninterrupted channel |
| failures | Deleted, private or region-locked videos are skipped. Expired URLs are fetched again, and playback resumes at the same point. A livestream that can't be played (e.g. members-only) is ignored. |

### Caveats

- YouTube changes often, and old yt-dlp versions stop working. If playback breaks, restart the container. yt-dlp needs a JavaScript runtime for YouTube, which the `deno` extra installs.
- Live detection is polled, so a livestream shows up up to `LIVE_POLL_INTERVAL` seconds late. Its end time in the guide is an estimate.
- Unless `ALWAYS_ON=true`, the first viewer waits a few seconds while stream URLs are fetched and ffmpeg starts.
- It's meant for watching on your own devices. Don't expose the stream publicly.

## Project Structure

```
partizan/
├── partizan/
│   ├── __main__.py      # entry point
│   ├── config.py        # environment variables
│   ├── youtube.py       # everything that talks to YouTube (yt-dlp)
│   ├── schedule.py      # the timeline: planning, live blocks, persistence
│   ├── channel.py       # glue: library and live loops, picks what plays now
│   ├── streamer.py      # ffmpeg commands, the pipeline, fan-out to viewers
│   ├── epg.py           # XMLTV and JSON guides
│   └── server.py        # HTTP endpoints, status page, HDHomeRun emulation
├── tests/               # schedule and output tests, plus an end-to-end stream test
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

### Tests

```sh
pip install -r requirements-dev.txt
python -m pytest
```

The end-to-end test runs the real server and ffmpeg against a fake YouTube backend serving local clips. It checks that the stream goes from videos to a live and back without timestamps jumping backwards.

## Contribute

This is a personal project, built in the open. Ideas, issues, and PRs are welcome.

## Credits

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [FFmpeg](https://ffmpeg.org). Not affiliated with YouTube or Google.

## License

[MIT](LICENSE)
