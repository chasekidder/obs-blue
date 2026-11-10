#!/bin/bash
# Fails the boot health check unless, for the stream user: stream-pilot left STARTING within 5 min
# of login AND obs.service, chromium.service and stream-pilot.service are all active.
set -u
user=stream
uid=$(id -u "$user" 2>/dev/null) || exit 0          # no stream user (e.g. image tests): don't block
for _ in $(seq 1 60); do
  [ -S "/run/user/$uid/bus" ] && break; sleep 5
done

run_as_stream() {
  runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"
}

run_as_stream /usr/bin/stream-pilot health --timeout 300 || exit 1

# A unit may be mid-restart (Restart=on-failure): retry for ~30 s before failing.
# `systemctl is-active a b c` succeeds if ANY unit is active, so check each one.
all_active() {
  local unit
  for unit in obs.service chromium.service stream-pilot.service; do
    run_as_stream systemctl --user is-active --quiet "$unit" || return 1
  done
}
for attempt in 1 2 3 4 5 6; do
  all_active && exit 0
  [ "$attempt" -lt 6 ] && sleep 5
done
echo "50-stream-pilot: obs.service, chromium.service and stream-pilot.service are not all active" >&2
exit 1
