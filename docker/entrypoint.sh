#!/bin/sh
# Container start: give /config to PUID:PGID, then run the command (normally `migratarr ui`).
# Nothing outside /config is changed; media folders keep their owners.
set -eu
CONFIG_DIR=${CONFIG_DIR:-/config}

if [ "$(id -u)" = 0 ]; then
    for name in PUID PGID; do
        eval "value=\${$name:-}"
        case "$value" in
            ''|*[!0-9]*) echo "migratarr: $name must be a number (got '$value')." >&2; exit 2 ;;
        esac
    done
    if [ "$PUID" = 0 ]; then
        echo "migratarr: running as root (PUID=0). Set PUID/PGID to the user that owns your media." >&2
    fi
    mkdir -p "$CONFIG_DIR"
    # Only files on /config's own filesystem: a read-only mount inside it (an SSH key) is left alone.
    find "$CONFIG_DIR" -xdev \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown -h "$PUID:$PGID" {} + \
        2>/dev/null || echo "migratarr: could not give every file in $CONFIG_DIR to $PUID:$PGID." >&2
fi
exec "$@"
