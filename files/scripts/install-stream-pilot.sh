#!/usr/bin/env bash
set -oue pipefail
export PIP_DISABLE_PIP_VERSION_CHECK=1
python3 -m venv --system-site-packages /usr/lib/stream-pilot/venv
/usr/lib/stream-pilot/venv/bin/pip install --no-cache-dir "/usr/src/stream-pilot[runtime]"
ln -sf /usr/lib/stream-pilot/venv/bin/stream-pilot /usr/bin/stream-pilot
rm -rf /usr/src/stream-pilot
# Playwright: no bundled browsers; it attaches to the system Chromium over CDP.
/usr/lib/stream-pilot/venv/bin/python -c "import stream_pilot, playwright, simpleobsws, StreamDeck, evdev, PIL, websockets"
