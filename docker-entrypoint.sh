#!/bin/sh
set -e
# YouTube changes often and old yt-dlp versions stop working; update on start unless disabled.
if [ "${UPDATE_YTDLP:-true}" = "true" ]; then
  echo "Updating yt-dlp..."
  pip install --quiet --no-cache-dir --upgrade "yt-dlp[default,deno]" || echo "yt-dlp update failed, continuing with the installed version"
fi
exec python -m partizan "$@"
