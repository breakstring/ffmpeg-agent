# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM ghcr.io/astral-sh/uv:0.12.8@sha256:d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a AS uv
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime-base

ARG TARGETARCH
COPY deploy/system-packages-${TARGETARCH}.tsv /tmp/system-packages.tsv
RUN apt-get update \
    && awk -F '\t' '{ print $1 "=" $2 }' /tmp/system-packages.tsv \
       | xargs apt-get install -y --no-install-recommends \
    && mkdir -p /usr/share/ffmpeg-agent \
    && dpkg-query -W -f='${Package}\t${Version}\n' > /usr/share/ffmpeg-agent/system-packages.tsv \
    && diff /tmp/system-packages.tsv /usr/share/ffmpeg-agent/system-packages.tsv \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python \
    && groupadd --gid 10001 media \
    && useradd --uid 10001 --gid media --no-create-home --shell /usr/sbin/nologin media \
    && mkdir -p /var/lib/ffmpeg-agent/state /var/lib/ffmpeg-agent/work /media/input /media/output \
    && chown -R media:media /var/lib/ffmpeg-agent /media
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEDIA_CONFIG_FILE=/run/secrets/media-config.json

FROM runtime-base AS service
ARG BUILD_REVISION=local
ENV MEDIA_IMAGE_VERSION=${BUILD_REVISION}
COPY --chown=media:media src/ ./src/
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2).close()"]
CMD ["python", "-m", "media_service"]
