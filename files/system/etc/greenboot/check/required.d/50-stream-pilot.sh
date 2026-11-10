#!/bin/bash
# Fails the boot health check unless the stream user's stream-pilot left STARTING within 5 min of login.
set -u
user=stream
uid=$(id -u "$user" 2>/dev/null) || exit 0          # no stream user (e.g. image tests): don't block
for _ in $(seq 1 60); do
  [ -S "/run/user/$uid/bus" ] && break; sleep 5
done
exec runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
  /usr/bin/stream-pilot health --timeout 300
