FROM python:3.14-slim

# ffmpeg: transcodes OTA channels (MPEG-2/AC-3) to browser-playable H.264/AAC.
# fonts-dejavu-core: the "VS"/"@" glyph drawn on match-logo composites (Pillow).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY . .

RUN mkdir -p /app/data /app/exports \
    && useradd --system --uid 10001 --home /app --shell /usr/sbin/nologin m3uboss \
    && chown -R m3uboss:m3uboss /app/data /app/exports

USER m3uboss

EXPOSE 43817

# Port is configurable via M3U_BOSS_PORT (default 43818). The Go edge
# container in front talks to us on this port over the media_net bridge.
ENV M3U_BOSS_PORT=43817
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('M3U_BOSS_PORT','43817')+'/healthz', timeout=3)" || exit 1
CMD ["sh","-c","exec uvicorn app.main:app --host 0.0.0.0 --port ${M3U_BOSS_PORT}"]
