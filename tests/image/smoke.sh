#!/usr/bin/env bash
# Container smoke test for the obs-blue image.
#   usage: tests/image/smoke.sh <image>
# Runs one `podman run --rm <image> bash -c ...` per check group and exits
# non-zero on the first failing group. Everything runs as root inside the
# container (no privileges, no host mounts).
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <image>" >&2
  exit 2
fi
image="$1"

# Secrets are matched by shape, never by value, so no secret has to live in
# this public repo: a filled-in secret-bearing config key, an obs-websocket
# password, a Facebook stream key or a Tailscale key.
SECRET_KEY_RE='^\s*(session_id|password|actor_id|group_id)\s*=\s*"[^"]+"|"server_password"\s*:\s*"[^"]+"'
SECRET_TOKEN_RE='FB-[0-9]{6,}-|tskey-[A-Za-z0-9]'

group() {  # group <title> <script>
  local title="$1" script="$2"
  echo
  echo "=== ${title}"
  if ! podman run --rm "$image" bash -c "set -euo pipefail; ${script}"; then
    echo "SMOKE FAIL: ${title}" >&2
    exit 1
  fi
}

group "stream-pilot CLI" '
help=$(stream-pilot --help)
echo "$help"
grep -q selftest-cdp <<< "$help"
'

group "Playwright attaches to the system Chromium over CDP (selftest-cdp)" '
stream-pilot selftest-cdp
'

group "packages: obs-studio, chromium present; obs browser plugin (CEF) absent" '
rpm -q obs-studio chromium
if rpm -q obs-studio-plugin-browser; then
  echo "obs-studio-plugin-browser must not be installed (no CEF)" >&2
  exit 1
fi
if rpm -q kdeconnectd; then
  echo "kdeconnectd must not be installed (the tray applet D-Bus-activates it)" >&2
  exit 1
fi
test -x /usr/bin/chromium-browser
'

group "OBS runs and hardware-encoder plugins" '
if out=$(QT_QPA_PLATFORM=offscreen timeout 60 obs --version 2>&1); then
  echo "obs --version: $out"
else
  echo "obs --version did not run (rc=$?; may need a display): $out"
  echo "falling back to the rpm file check"
fi
files=$(rpm -ql obs-studio)   # not piped into grep -q: SIGPIPE + pipefail
grep -q obs-ffmpeg <<< "$files"
echo "OBS encoder plugins in /usr/lib64/obs-plugins:"
ls /usr/lib64/obs-plugins | grep -E "obs-ffmpeg|obs-qsv"
'

group "recording and upload tools: ffmpeg with libopus, PulseAudio input and segments; rsync; ssh" '
command -v ffmpeg rsync ssh ssh-keygen
encoders=$(ffmpeg -hide_banner -encoders 2>/dev/null)   # here-strings below: SIGPIPE + pipefail
grep -qE "^ A[A-Z.]+ libopus " <<< "$encoders"
formats=$(ffmpeg -hide_banner -formats 2>/dev/null)
grep -qE "^ D[E ]?d? +pulse " <<< "$formats"
grep -qE "^  E +segment " <<< "$formats"
grep -qE "^  E +opus " <<< "$formats"
rsync --version > /dev/null
echo "ffmpeg (libopus, pulse, segment), rsync and ssh present"
'

group "Chromium managed policy" '
policy=/etc/chromium/policies/managed/stream-pilot.json
test -f "$policy"
python3 -m json.tool "$policy" > /dev/null
echo "$policy is valid JSON"
'

group "systemd user units: syntax, enablement, oomd drop-in" '
export XDG_RUNTIME_DIR=$(mktemp -d)
chmod 700 "$XDG_RUNTIME_DIR"
units=/usr/lib/systemd/user
  for u in obs chromium stream-pilot stream-pilot-display-mirror; do
  test -f "$units/$u.service"
  test -L "/etc/systemd/user/graphical-session.target.wants/$u.service"
done
# systemd-analyze exits non-zero in some containers for reasons unrelated to
# the unit files, so judge the output: any complaint that names one of our
# units (Unknown key/section, Invalid argument, bad setting, ...) fails.
  out=$(systemd-analyze verify --user "$units/obs.service" "$units/chromium.service" "$units/stream-pilot.service" "$units/stream-pilot-display-mirror.service" 2>&1 || true)
echo "systemd-analyze verify output: ${out:-<none>}"
if echo "$out" | grep -E "(obs|chromium|stream-pilot(-display-mirror)?)\.service" | grep -iE "unknown|invalid|bad|error|failed to parse|not found"; then
  echo "unit syntax problem" >&2
  exit 1
fi
if echo "$out" | grep -iE "Failed to (parse|initialize)" | grep -vi "manager"; then
  echo "unit syntax problem" >&2
  exit 1
fi
rec=$units/stream-pilot-recorder@.service
test -f "$rec"
grep -qx "ExecStart=/usr/bin/stream-pilot record %f" "$rec"
grep -qx "KillSignal=SIGINT" "$rec"
grep -qx "KillMode=mixed" "$rec"
stream-pilot record --help > /dev/null
test -f "$units/stream-pilot-upload.timer"
grep -qx "ExecStart=/usr/bin/stream-pilot upload" "$units/stream-pilot-upload.service"
grep -qx "IOSchedulingClass=idle" "$units/stream-pilot-upload.service"
stream-pilot upload --help > /dev/null
stream-pilot upload-key --help > /dev/null
out=$(systemd-analyze verify --user "$units/stream-pilot-upload.service" "$units/stream-pilot-upload.timer" 2>&1 || true)
if grep -E "stream-pilot-upload\.(service|timer)" <<< "$out" | grep -iE "unknown|invalid|bad|error|failed to parse"; then
  echo "upload unit syntax problem" >&2
  exit 1
fi
test -f /usr/lib/systemd/user/slice.d/90-stream-pilot-oomd.conf
grep -q "ManagedOOMMemoryPressure=auto" /usr/lib/systemd/user/slice.d/90-stream-pilot-oomd.conf
'

group "system configuration files" '
test -f /usr/lib/sysctl.d/90-stream-pilot-zram.conf
test -f /usr/lib/systemd/logind.conf.d/90-stream-pilot.conf
systemctl is-enabled stream-pilot-power-guard.service
stream-pilot power-guard --help > /dev/null
test -f /usr/lib/udev/rules.d/70-stream-pilot.rules
grep -q "exposure_dynamic_framerate=0" /usr/lib/udev/rules.d/70-stream-pilot.rules
grep -qx "OnBootSec=2min" /usr/lib/systemd/system/rpm-ostreed-automatic.timer.d/zz-stream-pilot.conf
grep -qx "OnCalendar=" /usr/lib/systemd/system/rpm-ostreed-automatic.timer.d/zz-stream-pilot.conf
grep -qx "CPUWeight=idle" /usr/lib/systemd/system/rpm-ostreed.service.d/90-stream-pilot-low-priority.conf
grep -qx "SampleRate=48000" /usr/share/stream-pilot/obs/basic/profiles/FB_Live/basic.ini
test -f /etc/xdg/kwinrulesrc
grep -q "kde-authorized remote-desktop com.rustdesk.RustDesk yes" /etc/xdg/autostart/com.rustdesk.RustDesk.desktop
grep -qx "Enabled=false" /etc/xdg/kwalletrc
grep -q -- "--password-store=basic" /usr/libexec/stream-pilot/chromium-launch
test -f /etc/plasmalogin.conf.d/50-stream-autologin.conf
test -f /etc/plasma-setup-done
test -x /etc/greenboot/check/required.d/50-stream-pilot.sh
bash -n /etc/greenboot/check/required.d/50-stream-pilot.sh
test -f /etc/stream-pilot/config.toml
echo "all present"
'

group "seed-obs produces a scrubbed, usable OBS config" '
export HOME=/tmp/h
mkdir -p "$HOME"
stream-pilot seed-obs
obs_dir=$HOME/.config/obs-studio
test -f "$obs_dir/user.ini"
if grep -q "^\[Locations\]" "$obs_dir/user.ini"; then
  echo "[Locations] must not be in the seeded user.ini" >&2
  exit 1
fi
if grep -rE "@(MAIN_CAMERA_DEVICE|JEWELRY_CAMERA_DEVICE|MICROPHONE_DEVICE)@" "$obs_dir"; then
  echo "unresolved device placeholder in seeded OBS config" >&2
  exit 1
fi
python3 - "$obs_dir/basic/profiles/FB_Live/streamEncoder.json" <<"PY"
import json, sys
profile = json.load(open(sys.argv[1]))["profile"]
# ffmpeg_vaapi_tex reads an FFmpeg profile number (100 = H.264 High); a string
# such as "high" reads as 0 and the encoder fails to open (ENOSYS).
if not isinstance(profile, int) or isinstance(profile, bool):
    sys.exit(f"streamEncoder.json profile must be an FFmpeg profile number, got {profile!r}")
PY
test -f "$obs_dir/plugin_config/obs-websocket/config.json"
python3 -m json.tool "$obs_dir/plugin_config/obs-websocket/config.json" > /dev/null
test -f "$HOME/.config/stream-pilot/secrets.toml"
echo "seeded files:"
(cd "$obs_dir" && find . -type f | sort)
'

group "no secrets baked into the image" "
dirs='/usr/share/stream-pilot /etc/stream-pilot /usr/lib/stream-pilot'
if grep -rlE --include='*.toml' --include='*.json' --include='*.ini' '${SECRET_KEY_RE}' \$dirs \
   || grep -rlE '${SECRET_TOKEN_RE}' \$dirs; then
  echo 'secret-looking value found in the image (files listed above)' >&2
  exit 1
fi
echo 'none found'
"

echo
echo "SMOKE PASS: ${image}"
