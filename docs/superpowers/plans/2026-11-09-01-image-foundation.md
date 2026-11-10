# Image Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the BlueBuild recipe into an image that boots the OptiPlex into an autologged-in, never-sleeping Plasma session. OBS (RPM), Chromium (RPM, managed policy, debugging port), and stream-pilot run as user services, with the memory, power-key, and update protections from the spec. The image must build locally and pass in-container smoke tests.

**Architecture:** Everything is image content: BlueBuild modules (`dnf`, `files`, `script`, `systemd`, `signing`) plus static files under `files/system/`. stream-pilot (from plan 2) is installed into a venv at `/usr/lib/stream-pilot/venv` at build time. Verification is a rootless `podman build`, then `podman run` checks against the built image. Booting a VM is out of reach (no sudo) and is left for the user.

**Tech Stack:** BlueBuild recipe v1 (`bluebuild` CLI at `/usr/local/bin/bluebuild`), rootless podman 5.8, Fedora Kinoite 44 (`ghcr.io/ublue-os/kinoite-main:44`), systemd, KDE Plasma 6 config (KConfig), Chromium enterprise policy, greenboot.

**Spec:** `docs/superpowers/specs/2026-11-09-stream-pilot-design.md`. **Depends on:** plan 2 (stream-pilot package) being complete.

## Global Constraints

- **Host safety:**
  - Never use `sudo`. Never run `rpm-ostree`, `bootc`, or `systemctl` against the host. Never enable or start any unit on the host.
  - Build only with rootless podman. Never `git push`, never push images. Keep at least 10 GB free in `/var/home` (`df -h /var/home`); prune dangling images after builds.
- Image files go under `files/system/` (copied to `/` by the `files` module). Build scripts go under `files/scripts/`.
- Secrets never enter the repo or the image: no stream key, websocket password, SSN session ID, Facebook IDs, cookies, password hashes, SSH keys, or Tailscale keys. Before every commit, run `git grep -nE '***REMOVED***|***REMOVED***|FB-[0-9]{6,}'`, which must return nothing.
- Chromium extension IDs:
  - SSN: `cppibjhfemifednoimlblfcmjgfhfjeg`
  - uBlock Origin Lite: `ddkjiahejlhfcafbddmgiahcphecmpfh`
  - Update URL: `https://clients2.google.com/service/update2/crx`
- CDP: `127.0.0.1:9222`, with profile dir `%h/.local/share/stream-pilot/chromium`.
- **Every KConfig key you write must be verified** against the `.kcfg`/`.desktop` sources inside the built base image, for example `podman run --rm <img> grep -r <Key> /usr/share/config.kcfg /usr/share/kservices6 /usr/share/kwin 2>/dev/null`. If a key name differs from this plan, use the real one and note it in the commit message.
- Commit after each task.

---

## File Structure

```
recipes/recipe.yml                                   (modify)
files/scripts/install-stream-pilot.sh                (create)
files/system/usr/lib/systemd/user/obs.service        (create)
files/system/usr/lib/systemd/user/chromium.service   (create)
files/system/usr/lib/systemd/user/stream-pilot.service (create)
files/system/usr/lib/systemd/user/obs-autostart.service      (delete)
files/system/usr/lib/systemd/user/chromium-autostart.service (delete)
files/system/var/lib/flatpak/extension/...           (delete whole tree)
files/system/usr/libexec/stream-pilot/chromium-launch (create, sh)
files/system/etc/chromium/policies/managed/stream-pilot.json (create)
files/system/etc/stream-pilot/config.toml            (create; non-secret defaults only)
files/system/usr/share/stream-pilot/obs/...          (create; scrubbed OBS defaults)
files/system/etc/xdg/{kscreenlockerrc,powerdevilrc,plasmanotifyrc,baloofilerc,kwinrc,kdeglobals,kwinrulesrc} (create)
files/system/etc/xdg/autostart/*.desktop             (create; Hidden=true overrides)
files/system/etc/sddm.conf.d/50-stream-autologin.conf (create; or plasmalogin equivalent)
files/system/usr/lib/systemd/logind.conf.d/90-stream-pilot.conf (create)
files/system/usr/lib/systemd/system/user@.service.d/90-stream-pilot-oomd.conf (create)
files/system/usr/lib/sysctl.d/90-stream-pilot-zram.conf (create)
files/system/usr/lib/udev/rules.d/70-stream-pilot.rules (create)
files/system/etc/greenboot/check/required.d/50-stream-pilot.sh (create)
.github/workflows/build.yml                          (modify: CDP smoke job)
.github/workflows/promote.yml                        (create)
iso/config.toml.example, iso/README.md, site.example/site.toml (create)
tests/image/smoke.sh                                 (create)
docs/superpowers/notes/base-image-facts.md           (create)
README.md                                            (modify)
```

---

### Task 1: Base-image facts and baseline build

**Files:** Create `docs/superpowers/notes/base-image-facts.md`

- [ ] **Step 1: Pull the base image and record facts** (rootless):

```bash
podman pull ghcr.io/ublue-os/kinoite-main:44
podman run --rm ghcr.io/ublue-os/kinoite-main:44 bash -c '
 rpm -q sddm plasma-login-manager python3 chromium obs-studio greenboot kdeconnect-kde plasma-discover-notifier intel-media-driver libva-intel-media-driver ffmpeg ffmpeg-free 2>&1
 rpm -qa | grep -iE "uupd|rpm-ostreed-automatic|ublue-os-update" ; ls /usr/lib/systemd/system | grep -iE "update|uupd|automatic"
 systemctl list-unit-files --root=/ | grep -iE "uupd|automatic|flatpak|display-manager|sddm|plasmalogin"
 cat /usr/lib/systemd/system/user@.service.d/*oomd* 2>/dev/null
 ls /usr/share/config.kcfg | head -100'
podman run --rm ghcr.io/ublue-os/kinoite-main:44 bash -c 'dnf -q repoquery --available obs-studio obs-studio-plugin-browser chromium greenboot greenboot-default-health-checks python3-evdev python3-pillow python3-websockets hidapi v4l-utils intel-media-driver libva-intel-media-driver 2>&1 | sort -u'
```

- [ ] **Step 2: Write the findings** to `docs/superpowers/notes/base-image-facts.md`:
  - the display manager (SDDM or Plasma Login Manager), the updater and its units, and the oomd defaults;
  - which packages are installed or available, and the Chromium binary path (`/usr/bin/chromium-browser` vs `/usr/bin/chromium`);
  - the decisions they force.

  Later tasks branch on this file. If `chromium_bin` differs from the plan 2 default (`/usr/bin/chromium-browser`), update `stream-pilot/src/stream_pilot/data/default-config.toml` in this task.
- [ ] **Step 3: Baseline build of the current recipe**, to prove the toolchain works before changing anything:

```bash
bluebuild generate -o /tmp/obs-blue.Containerfile recipes/recipe.yml
podman build -f /tmp/obs-blue.Containerfile -t localhost/obs-blue:baseline .
```

  If `bluebuild generate` needs flags or a different invocation in this CLI version, use `bluebuild generate --help` and document the working command in the facts file. Do not use `bluebuild build` with push options.
- [ ] **Step 4: Commit** `docs: record base image facts`.

---

### Task 2: Recipe packages and cleanup

**Files:** Modify `recipes/recipe.yml`; delete `files/system/usr/lib/systemd/user/obs-autostart.service`, `files/system/usr/lib/systemd/user/chromium-autostart.service`, and `files/system/var/lib/flatpak/`

- [ ] **Step 1: Edit the recipe.**
  - In the `dnf` module, install:
    - `micro`, `starship`, `tailscale` (kept),
    - `obs-studio`, `chromium`, `v4l-utils`, `hidapi`,
    - `python3-evdev`, `python3-pillow`, `python3-websockets`, `python3-pip`,
    - `greenboot` and `greenboot-default-health-checks` (if available per the facts),
    - the Intel VAAPI driver package named in the facts file.
  - Remove `firefox`, `firefox-langpacks`, `kdeconnect-kde` (if installed), and `plasma-discover-notifier` (if installed and removable without breaking deps; otherwise hide its autostart in Task 3).
  - In `default-flatpaks`, drop all system installs (Firefox, Gwenview, OBS, Chromium). Keep the module only if RustDesk is added: add `com.rustdesk.RustDesk` as a system Flatpak.
  - In the `systemd` user section, replace the old units with `obs.service`, `chromium.service`, and `stream-pilot.service` enabled. Keep the system `flatpak-system-update.timer` disabled.
  - Add the install step for stream-pilot (Task 5).
- [ ] **Step 2: Delete the old units and the flatpak extension tree.**
- [ ] **Step 3: Build** (same commands as Task 1, tag `localhost/obs-blue:dev`). Then verify:

```bash
podman run --rm localhost/obs-blue:dev rpm -q obs-studio chromium v4l-utils hidapi python3-evdev
```

- [ ] **Step 4: Commit** `feat(image): switch OBS and Chromium to RPMs, drop unused flatpaks`.

---

### Task 3: Session settings: autologin, no lock, no sleep, power key, Plasma trims

**Files:** Create every `/etc/xdg/*` file, the autostart overrides, the display-manager autologin drop-in, and `logind.conf.d/90-stream-pilot.conf` (listed in File Structure).

- [ ] **Step 1: Write the files.** Starting contents follow; verify every key per the Global Constraints.

`files/system/etc/xdg/kscreenlockerrc`
```ini
[Daemon]
Autolock=false
LockOnResume=false
Timeout=0
```

`files/system/etc/xdg/powerdevilrc` (Plasma 6 profile groups; `0` = no action)
```ini
[AC][Display]
DimDisplayWhenIdle=false
TurnOffDisplayWhenIdle=false

[AC][SuspendAndShutdown]
AutoSuspendAction=0
PowerButtonAction=0
LidAction=0
```

`files/system/etc/xdg/baloofilerc`
```ini
[Basic Settings]
Indexing-Enabled=false
```

`files/system/etc/xdg/kdeglobals`
```ini
[KDE]
AnimationDurationFactor=0
```

`files/system/etc/xdg/kwinrc`
```ini
[Plugins]
blurEnabled=false
contrastEnabled=false
slidingpopupsEnabled=false
kwin4_effect_translucencyEnabled=false
wobblywindowsEnabled=false
```

`files/system/etc/xdg/plasmanotifyrc`: enable Do Not Disturb permanently, or turn popups off. Find the real keys in `/usr/share/config.kcfg/notifications.kcfg` (or the equivalent) and write them here.

Autostart overrides: for each autostart that exists in the base image and should not run (Discover notifier, KDE Connect, `org.kde.kalendarac`, `baloo_file`), create a same-named file in `files/system/etc/xdg/autostart/` holding the original `[Desktop Entry]` keys plus `Hidden=true`. The `files` module overwrites the packaged file. List the actual names with `podman run --rm localhost/obs-blue:dev ls /etc/xdg/autostart`.

Autologin. For SDDM: `files/system/etc/sddm.conf.d/50-stream-autologin.conf`
```ini
[Autologin]
User=stream
Session=plasma
Relogin=false
```
If the facts file says Plasma Login Manager, write its equivalent config instead (find the config path and keys in its package files) and record the source in the commit message.

`files/system/usr/lib/systemd/logind.conf.d/90-stream-pilot.conf`
```ini
[Login]
HandlePowerKey=ignore
HandlePowerKeyLongPress=poweroff
```

- [ ] **Step 2: Build, then check** that each file is present in the image and that each key exists in the matching kcfg (one `grep` per key). Fix any mismatches.
- [ ] **Step 3: Commit** `feat(image): kiosk session defaults (autologin, no lock/sleep, power key, trims)`.

---

### Task 4: Memory, devices, and window rules

**Files:** Create the sysctl, oomd drop-in, udev rules, and `kwinrulesrc`.

`files/system/usr/lib/sysctl.d/90-stream-pilot-zram.conf`
```ini
vm.swappiness = 180
vm.page-cluster = 0
vm.watermark_boost_factor = 0
vm.watermark_scale_factor = 125
```

`files/system/usr/lib/systemd/system/user@.service.d/90-stream-pilot-oomd.conf`
```ini
[Service]
ManagedOOMMemoryPressure=auto
```
(This must sort after Fedora's oomd user drop-in named in the facts file, so it overrides it.)

`files/system/usr/lib/udev/rules.d/70-stream-pilot.rules`
```
# Elgato Stream Deck (vendor 0fd9): seat user access for hidapi
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
# ACPI power button event devices: read by stream-pilot (evdev)
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="Power Button", TAG+="uaccess"
```

`files/system/etc/xdg/kwinrulesrc` defines two rules:
- **OBS:** match window class `com.obsproject.Studio` (exact; check the RPM's app_id via the `StartupWMClass`/desktop file name in `/usr/share/applications`). Force position `0,0`, size = left 70% of a 1920x1080 screen (`1344,1080`), no border, and not "keep above."
- **Comments popout:** match window class by substring `chrome-www.facebook.com__live_producer_dashboard`. Force position `1344,0`, size `576,1080`, no border, keep above, and skip the taskbar.

  Use KWin's rule keys (`wmclass`, `wmclassmatch`, `position`, `positionrule=2`, `size`, `sizerule=2`, `noborder`, `noborderrule=2`, `above`, `aboverule=2`, `skiptaskbar`, `skiptaskbarrule=2`). Verify them against `/usr/share/kwin*/` or the KWin rules kcfg in the image.

  Write a `[General] count=2 rules=1,2` header. Put a comment at the top of the file: the geometry assumes 1920x1080, and stream-pilot's config has no override for it.

- [ ] **Step 1: Write the files. Step 2: Build and check presence;** run `udevadm verify` on the rules file inside the container if that subcommand is available. **Step 3: Commit** `feat(image): zram tuning, oomd override, device access, window rules`.

---

### Task 5: Chromium, OBS, and stream-pilot units, plus the stream-pilot install

**Files:** Create `files/scripts/install-stream-pilot.sh`, the three units, `files/system/usr/libexec/stream-pilot/chromium-launch`, `files/system/etc/chromium/policies/managed/stream-pilot.json`, `files/system/etc/stream-pilot/config.toml`, and the OBS defaults under `files/system/usr/share/stream-pilot/obs/`.

- [ ] **Step 1: Get stream-pilot into the build.**
  - Add a recipe `containerfile` module snippet `COPY stream-pilot /usr/src/stream-pilot`, before a `script` module that runs `install-stream-pilot.sh`.
  - If BlueBuild's build context doesn't include the repo root, use the fallback: move the copy step to `files/stream-pilot` via a build-time script that copies from `/tmp/files/...`. Document which one works in the facts file.

`files/scripts/install-stream-pilot.sh`
```bash
#!/usr/bin/env bash
set -oue pipefail
python3 -m venv --system-site-packages /usr/lib/stream-pilot/venv
/usr/lib/stream-pilot/venv/bin/pip install --no-cache-dir "/usr/src/stream-pilot[runtime]"
ln -sf /usr/lib/stream-pilot/venv/bin/stream-pilot /usr/bin/stream-pilot
rm -rf /usr/src/stream-pilot
# Playwright: no bundled browsers; it attaches to the system Chromium over CDP.
/usr/lib/stream-pilot/venv/bin/python -c "import stream_pilot, playwright, simpleobsws, StreamDeck, evdev, PIL, websockets"
```

`files/system/usr/libexec/stream-pilot/chromium-launch`
```sh
#!/bin/sh
exec "${CHROMIUM_BIN:-/usr/bin/chromium-browser}" \
  --user-data-dir="$HOME/.local/share/stream-pilot/chromium" \
  --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
  --hide-crash-restore-bubble --no-first-run --no-default-browser-check \
  --disable-backgrounding-occluded-windows --disable-renderer-backgrounding \
  --disable-background-timer-throttling --ozone-platform=wayland \
  "$@"
```
(Use the Chromium path from the facts file. Mark the file executable in git: `git update-index --chmod=+x`.)

`files/system/usr/lib/systemd/user/chromium.service`
```ini
[Unit]
Description=Chromium for stream-pilot
PartOf=graphical-session.target
After=graphical-session.target xdg-desktop-portal.service

[Service]
ExecStartPre=-/usr/bin/stream-pilot chromium-prep
ExecStart=/usr/libexec/stream-pilot/chromium-launch about:blank
Restart=on-failure
RestartSec=3
OOMScoreAdjust=500
MemoryHigh=3G

[Install]
WantedBy=graphical-session.target
```

`files/system/usr/lib/systemd/user/obs.service` (use the OBS binary from the RPM; check `rpm -ql obs-studio | grep bin/`)
```ini
[Unit]
Description=OBS Studio for stream-pilot
PartOf=graphical-session.target
After=graphical-session.target xdg-desktop-portal.service

[Service]
ExecStartPre=/usr/bin/stream-pilot seed-obs
ExecStart=/usr/bin/obs --disable-shutdown-check --profile "FB Live" --collection "Untitled"
Restart=on-failure
RestartSec=3
MemoryLow=1G

[Install]
WantedBy=graphical-session.target
```

`files/system/usr/lib/systemd/user/stream-pilot.service`
```ini
[Unit]
Description=stream-pilot controller
PartOf=graphical-session.target
After=graphical-session.target obs.service chromium.service
Wants=obs.service chromium.service

[Service]
ExecStart=/usr/bin/stream-pilot run
Restart=always
RestartSec=2

[Install]
WantedBy=graphical-session.target
```

`files/system/etc/chromium/policies/managed/stream-pilot.json`
```json
{
  "ExtensionInstallForcelist": [
    "cppibjhfemifednoimlblfcmjgfhfjeg;https://clients2.google.com/service/update2/crx",
    "ddkjiahejlhfcafbddmgiahcphecmpfh;https://clients2.google.com/service/update2/crx"
  ],
  "RestoreOnStartup": 1,
  "HomepageLocation": "about:blank",
  "NewTabPageLocation": "about:blank",
  "SyncDisabled": true,
  "TranslateEnabled": false,
  "SpellcheckEnabled": false,
  "NetworkPredictionOptions": 2,
  "BackgroundModeEnabled": false,
  "PasswordManagerEnabled": false,
  "MemorySaverModeSavings": 0,
  "HighEfficiencyModeEnabled": false,
  "DefaultBrowserSettingEnabled": false,
  "PromotionsEnabled": false,
  "MetricsReportingEnabled": false
}
```
(`RestoreOnStartup: 1` means "open New Tab page", which is `about:blank` here. Drop any key Chromium reports as unknown in `chrome://policy` during the smoke test, and note it in the commit.)

`files/system/etc/stream-pilot/config.toml`: a header comment saying site values go in `~/.config/stream-pilot/site.toml`, plus `[cdp] chromium_bin` set to the real path from the facts file. No secrets.

- [ ] **Step 2: OBS defaults (scrubbed).** Read the export at `/var/home/chase/repos/obs-blue/orig_scripts/obs-export/obs-studio/` (read-only; copy nothing secret). Create:
  - `usr/share/stream-pilot/obs/basic/profiles/FB_Live/basic.ini`: copy the export's FB_Live `basic.ini`, then:
    - `[AdvOut] Encoder=ffmpeg_vaapi_tex`, `AudioEncoder=ffmpeg_aac`, `RecEncoder=none`.
    - Remove `[Panels] CookieId` and the `[Stats]` geometry.
  - `usr/share/stream-pilot/obs/basic/profiles/FB_Live/streamEncoder.json`: `{"rate_control": "CBR", "bitrate": 6000, "keyint_sec": 2, "profile": "high"}`.
  - `usr/share/stream-pilot/obs/basic/profiles/FB_Live/service.json`: `{"type": "rtmp_custom", "settings": {"server": "", "key": "", "use_auth": false, "bwtest": false}}`.
  - `usr/share/stream-pilot/obs/basic/scenes/Untitled.json`: the export's `Untitled.json`, with `device_id` `/dev/video0` → `@MAIN_CAMERA_DEVICE@` and `/dev/video2` → `@JEWELRY_CAMERA_DEVICE@`. Everything else is kept.
  - `usr/share/stream-pilot/obs/user.ini`: the export's `user.ini` with the whole `[Locations]` section removed, `ExtraBrowserDocks` removed, `DockState`/`geometry` removed, and `WarnBeforeStartingStream=false`/`WarnBeforeStoppingStream=false` kept.
  - No `global.ini`, no `plugin_config/`, no `.bak`/`.v1` files, no logs.

  Run the secret grep from Global Constraints over `files/`.
- [ ] **Step 3: Build, then check in the container:**

```bash
podman run --rm localhost/obs-blue:dev bash -c '
 stream-pilot --help && systemd-analyze verify --user /usr/lib/systemd/user/{obs,chromium,stream-pilot}.service;
 HOME=/tmp/h stream-pilot seed-obs && ls -R /tmp/h/.config/obs-studio | head -40 && cat /tmp/h/.config/obs-studio/plugin_config/obs-websocket/config.json'
```

  `systemd-analyze verify` may warn about the missing graphical session in a container. Errors about unit syntax must be fixed.
- [ ] **Step 4: Commit** `feat(image): user units, chromium policy, obs defaults, stream-pilot install`.

---

### Task 6: Update promotion and greenboot rollback

**Files:** Create `.github/workflows/promote.yml`, `files/system/etc/greenboot/check/required.d/50-stream-pilot.sh`; modify `README.md`

`files/system/etc/greenboot/check/required.d/50-stream-pilot.sh` (executable)
```bash
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
```

**Updater:** configure the updater named in the facts file to download and stage only, never reboot, and never update Flatpaks. For `rpm-ostreed-automatic`, that's `/etc/rpm-ostreed.conf` `[Daemon] AutomaticUpdatePolicy=stage` plus enabling `rpm-ostreed-automatic.timer`. For `uupd`, disable its timer and enable `rpm-ostreed-automatic.timer` with the stage policy instead, or use `bootc-fetch-apply-updates.timer` with a drop-in that removes `--apply`. Write the chosen files under `files/system/` and record the choice in the facts file.

If the facts file shows greenboot isn't available or doesn't support this base, skip the package. Keep the script under `/usr/libexec/stream-pilot/healthcheck` instead, and document `bootc rollback` in the README.

`.github/workflows/promote.yml`: `workflow_dispatch` with a required input `digest` (`sha256:...`). The job:
- logs into ghcr with `GITHUB_TOKEN` (`packages: write`, `id-token: write`),
- installs cosign (`sigstore/cosign-installer@v3`),
- runs `skopeo copy --all docker://ghcr.io/${{ github.repository_owner }}/obs-blue@${{ inputs.digest }} docker://ghcr.io/${{ github.repository_owner }}/obs-blue:stable`,
- then `cosign sign --yes --key env://COSIGN_PRIVATE_KEY ghcr.io/${{ github.repository_owner }}/obs-blue@${{ inputs.digest }}` with `COSIGN_PRIVATE_KEY: ${{ secrets.SIGNING_SECRET }}`.

README: replace the rebase instructions' `:latest` with `:stable`. Add a "Promoting a build" section and a "First run on the stream PC" checklist covering:
- Facebook login,
- SSN import plus the two API toggles,
- RustDesk approval,
- camera and mic IDs into `~/.config/stream-pilot/site.toml`,
- private test-Group dry run.

- [ ] Steps:
  1. Write the files.
  2. Validate the YAML with `python3 -c "import yaml,sys;yaml.safe_load(open(sys.argv[1]))" .github/workflows/promote.yml`. If PyYAML is absent, use `podman run --rm -v "$PWD":/w:Z docker.io/mikefarah/yq e . /w/.github/workflows/promote.yml`.
  3. Build and check the script is present and executable.
  4. Commit `feat(image): stable-tag promotion and greenboot health check`.

---

### Task 7: ISO and site config scaffolding

**Files:** Create `iso/config.toml.example`, `iso/README.md`, `site.example/site.toml`

- `site.example/site.toml`: every user-level value with empty strings and comments:
  - `[facebook] actor_id/group_id/group_name`
  - `[ssn] session_id`
  - `[cameras.main] device`, `[cameras.jewelry] device`
  - `[obs]` is generated, so don't put it here.
- `iso/config.toml.example`: a bootc-image-builder config with `[customizations.installer.kickstart] contents = """..."""`. The kickstart:
  - does `zerombr`, `clearpart --all --initlabel`, `autopart`,
  - creates `user --name=stream --groups=wheel --iscrypted --password=@PASSWORD_HASH@`, plus `sshkey --username=stream "@SSH_PUBKEY@"`,
  - in `%post`: writes `/var/home/stream/.config/stream-pilot/site.toml` from `@SITE_TOML@`, chowns it, and runs `tailscale up --authkey=@TAILSCALE_AUTHKEY@ --hostname=stream-pc || true` (deferred via a oneshot if `tailscaled` isn't running in `%post`).
- `iso/README.md`: how to copy `site.example/` to `site/`, fill it in, render `iso/config.toml` (a small `sed` recipe), and build the ISO. That needs rootful podman, so it runs on the user's machine:
  `sudo podman run --rm -it --privileged --pull=newer --security-opt label=type:unconfined_t -v ./iso/config.toml:/config.toml:ro -v ./output:/output -v /var/lib/containers/storage:/var/lib/containers/storage quay.io/centos-bootc/bootc-image-builder:latest --type anaconda-iso --rootfs btrfs ghcr.io/chasekidder/obs-blue:stable`.
  **Do not run this yourself.**
- [ ] Steps: write the files; check `.gitignore` has `site/` and `output/` (add `output/`, `*.iso` exists); commit `docs(iso): kickstart and site config scaffolding`.

---

### Task 8: Image smoke test and CI CDP check

**Files:** Create `tests/image/smoke.sh`, `stream-pilot/src/stream_pilot/selftest.py`; modify `stream-pilot/src/stream_pilot/__main__.py` (add a `selftest-cdp` subcommand) and `.github/workflows/build.yml`

`selftest.py`: `async def selftest_cdp(chromium_bin: str) -> int`:
1. Start `chromium_bin --headless=new --remote-debugging-port=9333 --user-data-dir=<tmp> about:blank`.
2. Poll `http://127.0.0.1:9333/json/version` for up to 20 s.
3. `playwright.async_api.async_playwright().chromium.connect_over_cdp("http://127.0.0.1:9333")`, open a page, `evaluate("1+1") == 2`, and create a CDP session to send `Browser.getVersion`.
4. Print the versions, kill Chromium, and return 0. Return 1 on any failure.

Add `--no-sandbox` only when running as root (container CI).

`tests/image/smoke.sh <image>`: runs `podman run --rm <image> bash -c '...'` checks and exits non-zero on the first failure:
- `stream-pilot --help`; `stream-pilot selftest-cdp`
- `rpm -q obs-studio chromium`; `test -f /etc/chromium/policies/managed/stream-pilot.json`
- `python3 -m json.tool` on the policy
- `systemd-analyze verify` on the three user units (fail only on syntax errors)
- `sysctl` file present; logind drop-in present; udev rules present
- `HOME=/tmp/h stream-pilot seed-obs` succeeds and produces no `[Locations]` in `user.ini`
- `grep -rE '***REMOVED***|***REMOVED***' /usr/share/stream-pilot /etc/stream-pilot` finds nothing
- `obs --version` runs, or at least `rpm -ql obs-studio | grep -q obs-ffmpeg`, plus `ls /usr/lib64/obs-plugins | grep -E "obs-ffmpeg|obs-qsv"` to show which hardware encoders exist. Print the list for the report.

`build.yml`: add a second job `smoke` (`needs: bluebuild`, `if: github.event_name != 'pull_request'`) that pulls `ghcr.io/${{ github.repository_owner }}/obs-blue:latest` and runs `bash tests/image/smoke.sh ghcr.io/${{ github.repository_owner }}/obs-blue:latest`.

- [ ] Steps:
  1. Write the files.
  2. Rebuild `localhost/obs-blue:dev`, then run `bash tests/image/smoke.sh localhost/obs-blue:dev`, which must pass. Fix the cause of any failure; never weaken a check to make it pass.
  3. `podman image prune -f`.
  4. Commit `test(image): container smoke test and CDP compatibility check`.
