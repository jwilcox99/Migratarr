#!/bin/sh
# The `migratarr` command in the container. Started as root (the default, and `docker exec`),
# it switches to PUID:PGID first, so every file it writes in /config belongs to that user.
set -eu
umask "${UMASK:-002}"
if [ "$(id -u)" = 0 ] && [ "${PUID:-0}" != 0 ]; then
    exec setpriv --reuid="$PUID" --regid="${PGID:-$PUID}" --clear-groups -- python -m migratarr.cli "$@"
fi
exec python -m migratarr.cli "$@"
