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
