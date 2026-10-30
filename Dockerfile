# syntax=docker/dockerfile:1
# Migratarr in a container, run like Radarr and Sonarr: the web UI on port 8765,
# settings and runs in /config, media under one shared mount (see docs/docker.md).

FROM python:3.13-slim AS build
WORKDIR /src
COPY . .
RUN pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.13-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title="Migratarr" \
      org.opencontainers.image.description="Tiered storage for Radarr and Sonarr libraries: plan, approve, then move" \
      org.opencontainers.image.source="https://github.com/jwilcox99/Migratarr" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

# openssh-client: NAS over SSH; tini: reaps the UI's background jobs; tzdata: TZ for local times.
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client tini tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# `migratarr` always runs as PUID/PGID, also through `docker exec` (docker/migratarr.sh).
COPY docker/entrypoint.sh /usr/local/bin/migratarr-entrypoint
COPY docker/migratarr.sh /usr/local/bin/migratarr
RUN chmod 0755 /usr/local/bin/migratarr-entrypoint /usr/local/bin/migratarr

ENV MIGRATARR_HOME=/config \
    MIGRATARR_HOST=0.0.0.0 \
    MIGRATARR_PORT=8765 \
    MIGRATARR_IN_CONTAINER=1 \
    HOME=/config \
    PUID=1000 \
    PGID=1000 \
    UMASK=002 \
    TZ=Etc/UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME /config
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MIGRATARR_PORT', '8765') + '/ping', timeout=4)"

ENTRYPOINT ["tini", "--", "migratarr-entrypoint"]
CMD ["migratarr", "ui"]
