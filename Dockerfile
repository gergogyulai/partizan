FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user with its own virtualenv so yt-dlp can self-update.
RUN useradd --create-home --uid 1000 app \
 && python -m venv /opt/venv \
 && mkdir /data && chown -R app:app /opt/venv /data
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && chown -R app:app /opt/venv
COPY partizan ./partizan
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

USER app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=4)"
ENTRYPOINT ["docker-entrypoint.sh"]
