# obs-blue stream-pilot design

Date: 2026-11-09
Status: Draft for review

## Goal

Replace the Debian streaming PC (`stream-pc`, Dell OptiPlex 7070) with this bootc image and make a Facebook Group live stream need almost no interaction:

1. On boot, OBS opens preconfigured for Facebook Live.
2. On boot, Chromium opens the Facebook Live Producer for the Group.
3. The Facebook live is set up automatically so the persistent stream key is valid.
4. When OBS and Facebook are both ready, one Stream Deck press goes live.
5. Once live, the comments are captured, recorded, and shown next to the OBS preview.
6. Pressing the PC power button ends the stream cleanly and powers off.

Success means a host can power on, wait for a green key, press it, run a 2-4 hour stream on 8 GB of RAM without stutter or crashes, and press the power button when done. Every comment ends up in a saved record.

## Constraints and findings

These come from `orig_scripts/` and the OBS export from `stream-pc` (logs from Aug–Nov 2026).

- **Hardware:** i5-9500 (6 cores), Intel UHD 630, one 8 GB DDR4-2666 DIMM (single channel; no spare RAM available). The iGPU shares that single memory channel.
- **The target is a Facebook Group, posted as a personal profile.** Facebook removed Groups from the Graph API in 2024, and SSN's API bridge only works for Pages. Live setup and comment capture therefore have to happen in the browser. This is a hard requirement; moving to a Page is out of scope.
- **The base image is Kinoite (Plasma 6), Wayland only.** xdotool and pysikuli (X11, screenshot matching) cannot be carried over.
- **OBS today:** Flatpak 32.2.1 (not v31 as `CK_README.txt` says), 1080x1920 portrait, 30 fps, x264 veryfast CBR 6000, libfdk AAC 160. It has a Custom Browser Dock ("Facebook Chat" = SSN dock), which forces OBS onto XWayland and runs an extra Chromium engine (CEF) inside OBS.
- **From the logs:**
  - one stream had 20% of frames lagged from rendering stalls,
  - OBS started with as little as 407 MB free,
  - OBS crashed at launch when the desktop portal wasn't ready,
  - cameras configured as `/dev/video0` and `/dev/video2` were often missing at startup,
  - OBS's own leak counter always reported 0.
- **The "OBS memory leak" is most likely memory pressure, not an OBS leak.** Three Chromium engines run on 8 GB: the dashboard (playing the stream preview for hours), the comments popout plus SSN, and OBS's CEF dock. I found no evidence of a leak specific to the OBS Flatpak.
- **Facebook already ends the live when OBS sends Stop Streaming** (this setting is on).
- **SSN misses comments unless the Facebook comments popout is open**, which is why it gets opened today.

## Architecture

Three systemd user services run in the autologged-in Plasma session:

| Unit | Runs | Restart |
|---|---|---|
| `obs.service` | OBS Studio (RPM) with `--disable-shutdown-check` | `on-failure` |
| `chromium.service` | Chromium (RPM) with a dedicated profile and a localhost-only debugging port | `on-failure` |
| `stream-pilot.service` | The Python controller | `always` |

stream-pilot is the only component with workflow logic. It talks to OBS over obs-websocket, to Chromium over the Chrome DevTools Protocol (Playwright `connect_over_cdp`), to the Stream Deck over USB HID, and to the power button over evdev. It reconnects to OBS or Chromium after either one restarts.

All units are `WantedBy=graphical-session.target` and ordered `After=graphical-session.target xdg-desktop-portal.service`. That fixes the OBS-crashes-at-launch race. `network-online.target` has no effect in user units, so it is not used.

## Image contents (BlueBuild recipe)

- **Base:** `ghcr.io/ublue-os/kinoite-main:44` (unchanged).
- **Add RPMs:**
  - `obs-studio` (no browser plugin),
  - `chromium`,
  - `v4l-utils`,
  - `hidapi`,
  - Python 3,
  - RustDesk (RPM or Flatpak, whichever passes the unattended test below).
- **stream-pilot:** installed to `/usr/lib/stream-pilot/` as a Python virtualenv built at image build time (Playwright without browser downloads, `simpleobsws`, `streamdeck`, `Pillow`, `evdev`, `aiohttp`). A `/usr/bin/stream-pilot` entry point also offers `stream-pilot status` and `stream-pilot retry` for SSH use.
- **Remove:** the Firefox, Gwenview, and Chromium Flatpaks, the uBlock Flatpak extension files, and the two existing autostart units.
- **Add files:**
  - `/usr/lib/systemd/user/{obs,chromium,stream-pilot}.service`
  - `/etc/chromium/policies/managed/stream-pilot.json`
  - `/etc/stream-pilot/config.toml` (non-secret defaults)
  - `/usr/share/stream-pilot/obs/` (default OBS profile and scene collection)
  - `/etc/xdg/kwinrulesrc` (window placement)
  - `/etc/xdg/` overrides that trim Plasma (see the memory plan)
  - `/usr/lib/systemd/logind.conf.d/stream-pilot.conf` (`HandlePowerKey=ignore`)
  - `/usr/lib/udev/rules.d/` rules giving the seat user access to the Stream Deck and the "Power Button" input device
  - `/usr/lib/sysctl.d/` zram tuning
- **Updates:** image updates are staged in the background and applied only on the next boot. Automatic reboots and Flatpak auto-updates are disabled.

## Session and first run

- **Install:** a bootc-image-builder ISO with a kickstart that wipes the disk, creates user `stream`, and enables autologin into Plasma.
- **Site config:** the kickstart and the site values (password hash, Facebook actor and group IDs, SSN session ID) come from a gitignored `site/` directory used only at ISO build time. None of it is committed or published.
- **First boot (once):**
  1. stream-pilot seeds `~/.config/obs-studio/` from `/usr/share/stream-pilot/obs/` if it does not exist. It generates an obs-websocket password into `~/.config/stream-pilot/secrets.toml` and writes it into the OBS websocket config.
  2. The status key shows `LOG IN` until the Chromium profile is logged into Facebook. The host logs in once, including 2FA. The session lives only in `~/.local/share/stream-pilot/chromium/`.
  3. The host imports the SSN settings backup (`.data`) into the extension once.
  4. RustDesk is approved once (see the hardware checks).

  After that, boots need no interaction.

## Chromium

- **Launch:** `--user-data-dir=~/.local/share/stream-pilot/chromium`. Chromium refuses a debugging port on the default profile, so a dedicated profile is required.
- **Flags:**
  - `--remote-debugging-address=127.0.0.1 --remote-debugging-port=9222`
  - `--hide-crash-restore-bubble`
  - `--disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling`, so the hidden comments popout keeps updating
  - `--ozone-platform=wayland`
- **No "Restore pages?" prompt:**
  - `ExecStartPre` rewrites `"exit_type":"Crashed"` to `Normal` and `"exited_cleanly":false` to `true` in the profile's `Preferences`.
  - Policy sets startup to "open specific pages" instead of restoring the session.
  - stream-pilot closes Chromium over CDP (`Browser.close`) before powering off.
- **Network gate:** a second `ExecStartPre` waits until `https://www.facebook.com` answers, retrying every second. This replaces the `ping google.com` loop. OBS does not wait for the network.
- **Managed policy:**
  - Force-install SSN and uBlock Origin Lite (MV3; classic uBlock Origin no longer runs in current Chromium).
  - Disable sync, translate, spellcheck, network prediction, background apps, password manager prompts, and Memory Saver tab discarding.
- **Windows:**

  | Window | Placement |
  |---|---|
  | Producer dashboard (main window) | Behind OBS |
  | Comments popout | Behind OBS, not minimized |
  | Chat window (`--app` style) | Borderless, always on top, pinned as a column to the right of the OBS preview |

  OBS gets a matching KWin rule for the rest of the screen, so the layout is fixed.

## OBS

- **Install:** the OBS RPM is built into the image, so it updates and rolls back with the OS. The pinned Flatpak is the fallback (see the hardware checks). Neither needs the browser plugin.
- **Defaults** in `/usr/share/stream-pilot/obs/` start from the exported `FB_Live` profile and `Untitled` scene collection, with these changes:
  - Stream encoder: `ffmpeg_vaapi_tex` (zero-copy VAAPI H.264), CBR 6000, keyframe every 2 s. QuickSync is the fallback, then x264 veryfast.
  - Audio encoder: `ffmpeg_aac` at 160, since libfdk AAC is not in the Fedora build.
  - Camera sources use `/dev/v4l/by-id/...` paths (main = Logitech, jewelry = second camera).
  - The `ExtraBrowserDocks` entry is removed.
  - Start/stop confirmation prompts are off.
  - The stream key and websocket password are left out.
- **Stream server and key:** stream-pilot reads both from the Facebook setup page on every boot and applies them with `SetStreamServiceSettings` (`rtmp_custom`). The key never enters the image or the repo.
- **Seeding is one-time.** Changes the host makes in OBS persist.

## stream-pilot

### Modules

| Module | Responsibility |
|---|---|
| `main.py` | asyncio entry point, state machine, wiring |
| `config.py` | Merge `/etc/stream-pilot/config.toml` and `~/.config/stream-pilot/*.toml` |
| `obs.py` | obs-websocket client: status, stream settings, start/stop, scenes, stats |
| `browser.py` | CDP connection, window and tab management, graceful close |
| `fb/selectors.py` | **Every** Facebook text, role, and selector in one place |
| `fb/producer.py` | Login check, live setup, key extraction, go live, live confirmation |
| `fb/comments.py` | Inject the comment watcher into the popout and receive comments through a CDP binding |
| `ssn_listener.py` | Subscribe to the SSN chat-listener websocket (channel 4) |
| `comment_log.py` | Append-only `comments.jsonl` per stream, dedupe, comparison report |
| `deck.py` | Stream Deck key rendering and input (thread → asyncio bridge) |
| `power.py` | evdev `KEY_POWER` listener |
| `memory.py` | Per-minute sampling and guard actions |
| `viewer/` | Local chat viewer page (aiohttp + SSE), used in phase 2 |

### States

| State | Status key | Exit condition |
|---|---|---|
| `STARTING` | grey `BOOT` | OBS websocket and CDP both respond |
| `NO_NET` | grey `NO NET` | facebook.com reachable |
| `LOGIN` | orange `LOG IN` | Producer page loads logged in |
| `SETUP` | blue `SETUP` | Facebook setup done, key applied to OBS, OBS streaming, Facebook shows the incoming preview |
| `READY` | green `GO LIVE` | Stream key pressed |
| `GOING_LIVE` | blinking blue | `End live video` visible |
| `LIVE` | red `LIVE` + elapsed time | Power button, or a 3 s long-press on the stream key |
| `ENDING` | red `ENDING` | OBS reports stopped, then power-off or back to `SETUP` |
| `ERROR` | orange, step name | Stream key or Retry key pressed → back to `LOGIN` |

### Facebook setup (`SETUP`)

On the producer URL for the configured Group, by role or text rather than by pixel:

1. Confirm the page shows the configured Group name.
2. Click `Go live`.
3. Choose `Streaming software`.
4. Open `Advanced settings` and turn on `Persistent stream key` if it is off. Check that end-live-when-stream-stops is on, if the setup page shows that setting.
5. Read the server URL and stream key, apply them to OBS, and start the OBS stream as a private pre-roll.
6. Wait until Facebook shows the incoming preview and `Go live` is enabled, then enter `READY`.
7. While in `READY`, if Facebook reports the preview lost or expired, re-run setup from step 1.

### Going live and live

1. Click `Go live`, get past the `Add title` interstitial, and wait for `End live video`.
2. Read the live video ID from the URL, create `~/streams/<date>-<live id>/`, and open the comments popout (`/live/producer/dashboard/<id>/COMMENTS/`) in its own window.
3. Start the comment watcher on the popout and refresh the SSN dock.
4. **Park the dashboard tab** on `about:blank`. Nothing needs it after this, because ending is done from OBS.
5. Bring OBS to the front.

### Ending

- **Power button while `LIVE`:** OBS `StopStream`, wait for the stopped event (10 s timeout), wait 5 s for Facebook to auto-end, write the stream's comparison report, close Chromium over CDP, stop OBS, then `systemctl poweroff`.
- **Power button in any other state:** graceful close, then power off. A hardware long-hold still forces power off.
- **Long-press on `LIVE`:** the same stream stop without powering off, then back to `SETUP`.

The power key only reaches stream-pilot because `HandlePowerKey=ignore` is set in logind and PowerDevil's power-button action is set to nothing.

### Stream Deck (6-key)

| | Col 1 | Col 2 | Col 3 |
|---|---|---|---|
| Row 1 | Status / Go Live | Autofocus toggle | Main Cam |
| Row 2 | Retry setup | (spare) | Jewelry Cam |

- **Camera keys** switch the OBS scene and enable continuous autofocus on that camera's by-id device with `v4l2-ctl`, matching the current Companion behavior.
- **Autofocus key** toggles autofocus on the active camera and shows its state.
- **Retry key** works in any state except `LIVE` and `GOING_LIVE`.
- **Spare key** shows `MEM` when the memory guard warns.

Companion is no longer used.

### Comments

- **Two captures** write to one `comments.jsonl` per stream. Each line is JSON: `captured_at`, `created_at` (when available), `source` (`fb_dom` | `ssn`), `fb_comment_id` (when available), `author`, `text`, `live_id`.
  - **`fb_dom`:** stream-pilot injects a MutationObserver into the comments popout. It reports each comment through a CDP `Runtime.addBinding` callback, deduped by Facebook comment ID. On every (re)load of the popout it rescans the visible history, so reloads lose nothing.
  - **`ssn`:** stream-pilot subscribes to SSN's chat-listener websocket (`wss://io.socialstream.ninja/join/<session>/4/1`). This needs "Enable remote API control" and "Send chat messages to API server" turned on in SSN. The comments pass through SSN's hosted relay, and the session ID is treated as a secret.
- **Phase 1:**
  - The chat window shows the SSN dock (`dock.html?session=…`).
  - Both captures record.
  - After each stream, `report.txt` lists comments seen by only one capture.
- **Phase 2:** when `fb_dom` has missed nothing SSN caught for 3 consecutive real streams, the chat window switches to the stream-pilot viewer (big legible name + text, fed from `comments.jsonl` over SSE, refilled from the file on reload) and SSN is removed.
- The `source` field leaves room for future sources such as Whisper transcripts without a format change.

### Errors

- Each Facebook step has a timeout and up to 3 attempts, with a page reload between attempts.
- On final failure, stream-pilot enters `ERROR` and saves a screenshot and the page HTML to `~/.local/state/stream-pilot/failures/<timestamp>-<step>/`.
- **If OBS crashes while live,** systemd restarts it and stream-pilot restarts the stream with the same key. The crash sends no stop notice, so Facebook keeps the live open through the gap.
- **If Chromium crashes while live,** systemd restarts it. stream-pilot reopens the popout and resumes capture (history rescan covers the gap). The stream itself is unaffected.
- All state changes and errors go to the journal (`journalctl --user -u stream-pilot`).

## Memory and performance plan (8 GB, single channel)

1. No CEF in OBS: the SSN dock moves to the pinned Chromium window, and OBS runs natively on Wayland.
2. The dashboard tab is parked once live (no hours of preview decoding).
3. Zero-copy VAAPI H.264 removes a GPU-to-CPU copy of every frame and most of the CPU load.
4. zram (Fedora default) plus `vm.swappiness=180` and `vm.page-cluster=0`.
5. Unit priorities:
   - `obs.service`: `MemoryLow=` protection.
   - `chromium.service`: soft `MemoryHigh=` ceiling and `OOMScoreAdjust=500`.
   - systemd-oomd configured so OBS is never its victim.
6. Plasma trimming:
   - Baloo, the Discover notifier, KDE Connect, and unneeded autostarts off.
   - KWin blur, transparency, and animations off.
7. Chromium trimming: only the windows listed above, and the policy settings above.
8. **Memory guard:** each minute, log `MemAvailable`, memory PSI, and the RSS of OBS, each Chromium renderer (labeled by URL), `kwin_wayland`, and `plasmashell` to the journal. Below a configurable floor, the spare key shows `MEM`. Auto-reloading the popout exists behind a config flag that is **off**, since the host must never lose comments. It may be turned on after phase 2 proves the history rescan.
9. **Cameras:** measure what the inactive MJPEG camera's decoding costs. Only if it is significant, close it on scene switch (about 1 s reopen delay).

## Secrets

These never go in the repo or the image:

- Facebook session (Chromium profile)
- stream key (read at runtime, stored only in OBS's home config)
- obs-websocket password (generated at first boot)
- SSN session ID and Facebook actor/group IDs (site config)
- user password hash (kickstart)

`orig_scripts/` (it contains live cookies, a stream key, and passwords) and `site/` are gitignored.

## Remote access and observability

- **Tailscale** (already in the image) plus SSH key access for the `stream` user.
- **RustDesk** for unattended desktop access, relying on the Plasma portal's saved screen-share permission. **NanoKVM** is the guaranteed fallback.
- `stream-pilot status` prints the state, the last error, memory readings, and the current live ID.

## Testing

- **Unit tests** (pytest) for the state machine, comment dedupe and report, config merging, and memory guard decisions, with fake OBS, browser, deck, and power-key adapters. They run in GitHub Actions alongside the image build.
- **Facebook flows** against a private test Group with only the owner as member:
  - `stream-pilot --dry-run` performs everything up to, but not including, clicking `Go live`.
  - A full run goes live in the test Group, checks parking, comment capture, and auto-end on stop.
- **Image behavior** in a VM from the ISO: autologin, unit ordering, policies, the power-key path, the no-restore-prompt path, and the zram/sysctl settings.
- **On hardware before the first public stream:** one test-Group rehearsal, then one real test stream.

## Rollout

1. On `stream-pc`, record `ls -l /dev/v4l/by-id/` and `pactl list short sources` to fill the camera and microphone IDs. The OBS export and SSN backups are already in `orig_scripts/`.
2. Take a full disk image of the Debian install, or install to a spare disk.
3. Build the ISO locally with the `site/` config and install it.
4. First-run checklist: Facebook login, SSN import and API toggles, RustDesk approval.
5. Test-Group dry run, full test-Group run, then the first public stream with the Debian disk image kept until three streams succeed.

## Verify on hardware (each has a fallback)

| Item | Fallback |
|---|---|
| Fedora OBS RPM exposes VAAPI (or QuickSync) H.264 on UHD 630 | Pinned OBS Flatpak (no CEF needed) |
| Facebook keeps a pre-roll preview alive long enough before `Go live` | Start the OBS stream on button press |
| Parked dashboard does not affect the live or auto-end | Keep the dashboard open, reload it every 30 min |
| SSN extension force-installs by policy in current Chromium | Ship it unpacked and load it at startup |
| Hidden (not minimized) popout keeps rendering with the anti-throttling flags | Keep the popout as a small visible strip |
| RustDesk unattended access on Plasma 6 Wayland | NanoKVM |
| KWin rules and Plasma trims apply from `/etc/xdg` | First-boot seeding into `~/.config` |
| Kinoite 44 login manager autologin (SDDM vs Plasma Login Manager) | Configure whichever ships |
| Base image updater can be set to stage-only, no reboot | Disable it and update manually over SSH |
| Stream Deck model is the 6-key Mini | Adjust the key layout table |

## Out of scope / future

- Moving the live to a Facebook Page (that would enable the Graph API for setup and comments).
- Whisper voice transcription and automated comment processing (claims, item numbers). The comment log format is ready for them.
- Network-level comment capture from Facebook's internal traffic (kept in reserve if DOM capture proves incomplete).
