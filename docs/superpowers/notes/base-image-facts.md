# Base image facts: `ghcr.io/ublue-os/kinoite-main:44`

Recorded 2026-11-09 from `ghcr.io/ublue-os/kinoite-main@sha256:b3f2da878171cd80c27ad5e583f30151f7992b6acc8846f86fc785d3e7641960` (rootless podman, no VM). Later tasks branch on this file.

## Display manager: Plasma Login Manager (not SDDM)

- `plasma-login-manager-6.8.2` is installed. `sddm` is not.
- `/etc/systemd/system/display-manager.service` points to `plasmalogin.service`.
- `fedora-kinoite-plasmalogin-workaround.service` is enabled. It fixes missing shadow entries on Kinoite.
- Config: `/etc/plasmalogin.conf` is SDDM-style, with an `[Autologin]` section (`User=`, `Session=`, `Relogin=`). A drop-in dir `/etc/plasmalogin.conf.d/` exists. PAM ships `plasmalogin-autologin`.
- The only Wayland session is `/usr/share/wayland-sessions/plasma.desktop`, so `Session=plasma` is used. There are no X11 sessions.
- **Decision:** autologin goes in `/etc/plasmalogin.conf.d/50-stream-autologin.conf` and not in `/etc/sddm.conf.d/`.

## Updater: `rpm-ostreed-automatic` (from `ublue-os-update-services`)

- `rpm-ostreed-automatic.timer` is enabled. ublue drop-ins run it daily at 04:00 on unmetered networks.
- `/etc/rpm-ostreed.conf` already has `[Daemon] AutomaticUpdatePolicy=stage`. That means download and stage only, applied on the next boot, never rebooting.
- `bootc-fetch-apply-updates.timer` is disabled. It would run `bootc upgrade --apply`, which reboots.
- There is no `uupd` in this base.
- Flatpak auto-update:
  - System: `flatpak-system-update.timer`, enabled by preset. The recipe disables it.
  - User: `flatpak-user-update.timer`, enabled by user preset. It must be disabled too (spec: "Flatpak auto-updates are disabled").
- **Decisions:**
  - Ship `/etc/rpm-ostreed.conf` with `AutomaticUpdatePolicy=stage`. This makes the policy explicit and survives a base change.
  - Keep `rpm-ostreed-automatic.timer` enabled and leave `bootc-fetch-apply-updates.timer` disabled.
  - Disable `flatpak-system-update.timer` and the user `flatpak-user-update.timer`.

## oomd defaults

- `/usr/lib/systemd/system/user@.service.d/` holds `00-uresourced.conf` (`Delegate=cpu io memory`) and `10-login-barrier.conf`.
- `/usr/lib/systemd/oomd.conf.d/10-oomd-defaults.conf` sets `DefaultMemoryPressureDurationSec=20s`.
- `systemd-oomd-defaults-259.10` is installed and `systemd-oomd.service` is enabled. The package ships:
  - `/usr/lib/systemd/system/system.slice.d/10-oomd-per-slice-defaults.conf`
  - `/usr/lib/systemd/user/slice.d/10-oomd-per-slice-defaults.conf`

  Both set `ManagedOOMMemoryPressure=kill` with `ManagedOOMMemoryPressureLimit=80%`. The `user/slice.d` file applies to **every slice inside the user manager** (`app.slice`, `session.slice`, `background.slice`), and that is where OBS, Chromium, and stream-pilot run.
- No oomd drop-in exists for `user@.service`.
- **Decision (this deviates from plan 1 Task 4):** the plan's `user@.service.d/90-stream-pilot-oomd.conf` would not override the per-slice kill setting. The override goes to `/usr/lib/systemd/user/slice.d/90-stream-pilot-oomd.conf` with `[Slice] ManagedOOMMemoryPressure=auto`. It sorts after `10-oomd-per-slice-defaults.conf`, so it wins. System slices keep Fedora's default, and the kernel OOM killer stays as the last resort.

## Packages

| Package | In base | Available (repo) |
|---|---|---|
| python3 | 3.14.10 | — |
| obs-studio | no | 32.1.1-3 (fedora updates); there is no negativo17 build |
| obs-studio-plugin-browser | no | available, **not installed** (no CEF, per spec) |
| obs-studio-plugin-vaapi | no | 0.4.2 (the GStreamer VAAPI plugin, not needed: OBS's built-in `obs-ffmpeg` has `ffmpeg_vaapi_tex`) |
| chromium | no | 156.0.8078.79 (fedora updates) |
| greenboot, greenboot-default-health-checks | no | 0.16.4 (fedora updates) |
| python3-evdev | no | 1.9.3 |
| python3-pillow | no | 12.3.0 |
| python3-websockets | no | 15.0.1 |
| python3-pip | no | 26.0.1 |
| hidapi | no | 0.15.0 |
| v4l-utils | no | 1.32.0 |
| VAAPI driver | `libva-intel-media-driver` 26.4.0 (negativo17 fedora-multimedia) | — (`intel-media-driver` doesn't exist) |
| ffmpeg | `ffmpeg` 8.1.3 (negativo17, full codecs) | `ffmpeg-free` is not installed |
| firefox, firefox-langpacks | installed | removed by the recipe |
| kdeconnect-kde | **not installed** | — |
| plasma-discover-notifier | installed | its autostart is hidden in Task 3 rather than removed, because removing it may pull out plasma-discover deps |
| RustDesk | no RPM in the repos | Flatpak `com.rustdesk.RustDesk` (see Task 2) |

Repos in the base image:
- fedora, updates, updates-archive, updates-testing (disabled)
- fedora-cisco-openh264
- negativo17-fedora-multimedia
- COPRs: ublue-os-akmods, rok-cdemu

Chromium binary path, OBS binary, and OBS encoder plugins are recorded in the "Built image" section below, after the packages are installed.

## Autostart entries in the base (`/etc/xdg/autostart`)

`at-spi-dbus-bus`, `baloo_file`, `geoclue-demo-agent`, `gmenudbusmenuproxy`, `kaccess`, `orca-autostart`, `org.kde.discover.notifier`, `org.kde.kdeconnect.daemon` (the entry exists even though kdeconnect-kde isn't installed), `org.kde.kunifiedpush-distributor`, `org.kde.plasma-fallback-session-restore`, `org.kde.plasmashell`, `org.kde.xwaylandvideobridge`, `pam_kwallet_init`, `polkit-kde-authentication-agent-1`, `powerdevil`, `spice-vdagent`, `vboxclient`, `vmware-user`, `xdg-user-dirs`, `xembedsniproxy`.

## KConfig schemas present (`/usr/share/config.kcfg`)

Relevant: `kscreenlockersettings.kcfg`, `kwin.kcfg`, `baloosettings.kcfg`, `notifications_*settings.kcfg` (`notifications_donotdisturbsettings.kcfg`, `notificationsettings.kcfg`, …), `workspaceoptions_*`. PowerDevil's schema isn't in `config.kcfg`; Task 3 finds it in the PowerDevil library/plugin data.

## Building locally (rootless)

Working commands with BlueBuild 0.9.36:

```bash
bluebuild generate -o /tmp/obs-blue.Containerfile recipes/recipe.yml
chcon -R -t container_file_t .bluebuild-scripts_*        # SELinux: see below
podman build -f /tmp/obs-blue.Containerfile -t localhost/obs-blue:dev .
```

- `bluebuild generate` writes a `.bluebuild-scripts_<hash>/` directory into the repo root. The build bind-mounts it into `RUN` steps, and it adds `/.bluebuild-scripts_*` to `.gitignore` on its own.
- With SELinux enforcing, those scripts are labeled `user_home_t`, and the build container can't execute them (`/scripts/pre_build.sh: Permission denied`, exit 126). Relabeling only that directory to `container_file_t` fixes it. No `--security-opt label=disable` and no privileges are needed.
- The build context is the repo root. The Containerfile `COPY`s `./files`, `./modules`, and `cosign.pub`, so a recipe `containerfile` snippet can `COPY stream-pilot ...` directly from the context.
- Baseline build of the unchanged recipe: succeeded, image `localhost/obs-blue:baseline`. The `default-flatpaks` module only writes config at build time; Flatpaks install on first boot.
- Never use `bluebuild build` (it pushes and signs).

## Decisions these facts force

1. **Autologin** goes to Plasma Login Manager (`/etc/plasmalogin.conf.d/`).
2. **Updates:** keep ublue's `rpm-ostreed-automatic` with an explicit `AutomaticUpdatePolicy=stage`. Disable both Flatpak update timers.
3. **greenboot** is available (0.16.4), so it is installed and the health check goes to `/etc/greenboot/check/required.d/`.
4. **VAAPI driver:** `libva-intel-media-driver` is already in the base and is listed explicitly in the recipe.
5. **No `kdeconnect-kde` to remove.** Its autostart is hidden anyway. `plasma-discover-notifier` is hidden, not removed.
6. **oomd:** the override goes in `/usr/lib/systemd/user/slice.d/90-stream-pilot-oomd.conf` and not in `user@.service.d/`.
7. **Module order:** `dnf` runs before `files`, so package files never overwrite our `/etc` overrides, and editing `files/` doesn't invalidate the package-install layer cache.

## Built image (Task 2)

Recorded from `localhost/obs-blue:dev` (recipe: dnf, files, default-flatpaks, systemd, signing). Packages as built: `obs-studio-32.1.1-3.fc44`, `chromium-156.0.8078.79-1.fc44`, `v4l-utils-1.32.0`, `hidapi-0.15.0`, `python3-evdev-1.9.3`, `python3-pillow-12.3.0`, `python3-websockets-15.0.1`, `python3-pip-26.0.1`, `greenboot-0.16.4` (+ `greenboot-default-health-checks`), `dejavu-sans-fonts-2.37`, `libva-intel-media-driver-26.4.0`. `firefox` is absent.

- **`obs-studio-plugin-browser` is pulled in as a weak dependency** of `obs-studio`, even though the recipe doesn't list it. The recipe therefore sets `install.exclude: [obs-studio-plugin-browser]`. In the built image `obs-browser.so`/`obs-browser-page` are absent (no CEF).
- **Python:** `python3` is 3.14.10.

### Chromium
- Binary: `/usr/bin/chromium-browser`, a symlink to `/usr/lib64/chromium-browser/chromium-browser.sh`. This is the only binary in `/usr/bin`. There is no `/usr/bin/chromium`. `[cdp] chromium_bin = "/usr/bin/chromium-browser"` in `default-config.toml` is correct.
- Desktop file: `/usr/share/applications/chromium-browser.desktop`, `StartupWMClass=Chromium-browser`.
- `/etc/chromium/` contains `chromium.conf`, `master_preferences`, `native-messaging-hosts/` and `policies/`. **`/etc/chromium/policies/managed` does NOT exist**, so the Task 3 policy file must create it. `files/` copies the directory tree, so shipping `files/system/etc/chromium/policies/managed/stream-pilot.json` is enough.

### OBS
- Binaries: `/usr/bin/obs`, `/usr/bin/obs-ffmpeg-mux`, `/usr/bin/obs-nvenc-test`.
- Desktop file: `/usr/share/applications/com.obsproject.Studio.desktop` (`Exec=obs`, `StartupWMClass=obs`).
- Plugins in `/usr/lib64/obs-plugins`: decklink, decklink-captions, decklink-output-ui, frontend-tools, image-source, linux-alsa, linux-capture, linux-jack, linux-pipewire, linux-pulseaudio, linux-v4l2, obs-ffmpeg, obs-filters, obs-libfdk, obs-nvenc, obs-outputs, obs-pwvideo, obs-qsv11, obs-transitions, obs-vst, obs-webkitgtk, obs-webrtc, obs-websocket, rtmp-services, text-freetype2, vlc-video.
- **obs-ffmpeg (VAAPI): present.** `strings obs-ffmpeg.so | grep -i vaapi` shows the encoder ids `ffmpeg_vaapi_tex`, `hevc_ffmpeg_vaapi_tex`, `av1_ffmpeg_vaapi_tex`, `hevc_ffmpeg_vaapi` and `av1_ffmpeg_vaapi`, plus the "FFmpeg VAAPI H.264 / HEVC / AV1" names and the `h264_vaapi` codec string. The plain H.264 ids are `ffmpeg_vaapi` and `ffmpeg_vaapi_tex`. They don't show up as standalone strings only because the linker merges them as tails of the `av1_`/`hevc_` ids, which is the usual upstream naming. The recipe-side VAAPI driver `iHD_drv_video.so` is in `/usr/lib64/dri`.
- **obs-qsv11 (QuickSync): present.** Encoder ids: `obs_qsv11`, `obs_qsv11_v2`, `obs_qsv11_soft`, `obs_qsv11_soft_v2`, plus the `_hevc` and `_av1` variants.
- **obs-x264: there is no `obs-x264.so`** in the RPM, and there is no `obs_x264` encoder id anywhere in `/usr/lib64` or `/usr/share/obs`. Software H.264 comes from the ffmpeg-based `libx264` path inside `obs-ffmpeg.so`, using the negativo17 `ffmpeg-libs` 8.1.3, which has `libx264`. Don't write an OBS profile that sets `StreamEncoder=obs_x264`. Use hardware encoders (`ffmpeg_vaapi_tex` / `obs_qsv11`) or the ffmpeg `libx264` encoder.
- `obs-nvenc` and `obs-libfdk` are also present and irrelevant on this Intel box.

### Fonts
- `fc-list | grep DejaVuSans-Bold` finds `/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf` (also `DejaVuSans-BoldOblique.ttf`). `dejavu-sans-fonts` was added to the recipe's install list explicitly.

### greenboot
- Units: `greenboot-healthcheck.service`, `greenboot-set-rollback-trigger.service`, `greenboot-success.target`, plus a `greenboot-healthcheck.service.d/10-network-online.conf` drop-in.
- Check dirs: `/etc/greenboot/check/{required.d,wanted.d}` both **exist** (empty), and `/usr/lib/greenboot/check/{required.d,wanted.d}` ship the defaults.
- `greenboot-default-health-checks` installs `/usr/lib/greenboot/check/required.d/01_repository_dns_check.sh` and `02_watchdog.sh`, and `wanted.d/01_update_platforms_check.sh`. **The DNS check is `required`**, so a PC with no network at boot may be rolled back. Task 4/6 should account for this, e.g. by deciding whether to mask or override it.

## Window rules and oomd (Task 4)

- **KWin 6.8.2 rules schema** (`rulesettings.kcfg` and `rulebooksettingsbase.kcfg` in the KWin 6.8.2 source, cross-checked against the key strings in `/usr/lib64/libkwin.so.6.8.2`; the rules schema is compiled in, so there is no kcfg under `/usr/share/config.kcfg`):
  - `wmclass`, `wmclasscomplete`, `wmclassmatch` (0 = unimportant, 1 = exact, 2 = substring, 3 = regexp), `types` (`NET::NormalMask` = 1), `position`, `size`, `above`, `skiptaskbar`, `skippager`, `skipswitcher`, and the matching `<key>rule` keys all exist (`Force` = 2).
  - **`noborder` is deprecated in 6.8.** KWin migrates `noborder`/`noborderrule` to `decorationpolicy` (`none`, `client-preference`, `server`, `shadow`) and `decorationpolicyrule`, and then rewrites the user's config. The shipped file uses `decorationpolicy=none`, `decorationpolicyrule=2`.
  - **`[General] count=`/`rules=` are legacy.** The current key is `Order=`. A non-empty `rules=` triggers a migration and a rewrite of `~/.config/kwinrulesrc` on first load. The shipped file uses `Order=1,2` only.
- **Cascade:** `RuleBookSettings` opens `KSharedConfig::openConfig("kwinrulesrc", KConfig::NoGlobals)`. That is a normal cascading config, so `/etc/xdg/kwinrulesrc` works as a default and `~/.config/kwinrulesrc` overrides it. In the image, `kreadconfig6 --file kwinrulesrc` reads the values from `/etc/xdg`. If the user ever edits rules in System Settings, the user file shadows the `Order` list, so a later task can seed `~/.config` at first boot if needed.
- **OBS app_id:** the desktop file is `com.obsproject.Studio.desktop`, and the rule matches `com.obsproject.Studio` exactly. Chromium `--app=` windows match by substring on `chrome-www.facebook.com__live_producer_dashboard`.
- **oomd:** the `user/slice.d` directory holds only `10-oomd-per-slice-defaults.conf`, and `systemd-analyze cat-config systemd/user/slice.d` in the built image lists `90-stream-pilot-oomd.conf` after it.
- `udevadm verify` exists (systemd 259) and passes for `70-stream-pilot.rules`. The four `vm.*` sysctls exist in `/proc/sys/vm`.

## stream-pilot install (Task 5)

- **COPY approach that worked:** the build context is the repo root, so a recipe `containerfile` module with `snippets: [COPY stream-pilot /usr/src/stream-pilot]`, placed after `files` and before a `script` module (`install-stream-pilot.sh`), works directly. No fallback through `/tmp/files` was needed. A repo-root `.containerignore` keeps `stream-pilot/.venv`, `__pycache__`, `*.egg-info`, `.pytest_cache`, `.superpowers`, `orig_scripts`, `site`, `output` and `*.iso` out of the context. The script deletes `/usr/src/stream-pilot`, but the `COPY` layer still holds the source (a few hundred KB, no secrets).
- **Venv:** `/usr/lib/stream-pilot/venv` (`--system-site-packages`, so Fedora's `python3-evdev`, `python3-pillow` and `python3-websockets` are used). It is 182 MB in total. `/usr/bin/stream-pilot` is a symlink into it. The import check (`stream_pilot, playwright, simpleobsws, StreamDeck, evdev, PIL, websockets`) passes with no extra system libraries. Playwright downloads no browsers (`~/.cache/ms-playwright` doesn't exist).
- **pip installs into the venv:** `stream-pilot 0.1.0`, `playwright 1.65.0`, `simpleobsws 1.4.3`, `streamdeck 0.10.0`, `greenlet 3.5.6`, `msgpack 1.2.4`. System packages are reused, not reinstalled: `evdev 1.9.3`, `pillow 12.3.0`, `websockets 15.0.1`.
- **User units:** `systemd: user: enabled:` creates the wants symlinks in `/etc/systemd/user/graphical-session.target.wants/{obs,chromium,stream-pilot}.service`. `systemd-analyze verify --user` passes with `XDG_RUNTIME_DIR` set to a 0700 dir (without it, the tool dies with "Failed to lookup RuntimeDirectory path").
- **Chromium wrapper:** `/usr/lib64/chromium-browser/chromium-browser.sh` sources `/etc/chromium/chromium.conf`. It adds `--disable-features=LensOverlay,...` and `--enable-features=...` (the Wayland features when `WAYLAND_DISPLAY` is set), and it does **not** force an ozone platform, so `--ozone-platform=wayland` in `chromium-launch` doesn't conflict. The wrapper pipes stdout/stderr through `cat`. The `exec` replaces the shell with Chromium, so the service's main process is still Chromium.
- **Chromium policy verification (Chromium 156, headless in the image, `chrome://policy` read over CDP):** all keys report `OK` except `HomepageLocation=about:blank`. That one reported **Error: "Invalid URL. Must be a URL with a standard scheme"**, so the key was dropped. `NewTabPageLocation=about:blank` is OK. No key is unknown or deprecated. Headless Chromium in the image needs `HOME=<writable dir>` (`/root` is a dangling bootc symlink), `--no-sandbox`, and `--remote-allow-origins=*` for the CDP websocket.
- **OBS defaults:** `/usr/share/stream-pilot/obs` holds `user.ini`, `basic/profiles/FB_Live/{basic.ini,service.json,streamEncoder.json}` and `basic/scenes/Untitled.json`. The scene's USB microphone `device_id` (`alsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback`) is hardware-specific. It is not a secret and is kept as in the export.
