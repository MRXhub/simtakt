#!/bin/sh
# Run the command as the simtakt account, renumbered to PUID/PGID.
#
# Tools such as OpenSSH look the current uid up in /etc/passwd and refuse to
# run when it is missing, so a bare `--user <uid>` that the image does not know
# breaks SSH workers. The container therefore starts as root, moves the one
# image account to the requested ids, and drops privileges before exec.
set -eu

if [ "$(id -u)" != "0" ]; then
    # Started with an explicit --user: keep that identity unchanged.
    exec "$@"
fi

PUID="${PUID:-10001}"
PGID="${PGID:-10001}"
case "$PUID$PGID" in
    *[!0-9]*|"") echo "entrypoint: PUID and PGID must be numeric ids" >&2; exit 64 ;;
esac
if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
    echo "entrypoint: refusing to run as root; set PUID/PGID to a non-zero id" >&2
    exit 64
fi

if [ "$(id -g simtakt)" != "$PGID" ]; then
    groupmod --non-unique --gid "$PGID" simtakt
fi
if [ "$(id -u simtakt)" != "$PUID" ]; then
    # usermod also re-owns the files inside the home directory.
    usermod --non-unique --uid "$PUID" --gid "$PGID" simtakt
fi
chown simtakt:simtakt /home/simtakt

export HOME=/home/simtakt USER=simtakt LOGNAME=simtakt
exec setpriv --reuid=simtakt --regid=simtakt --init-groups -- "$@"
