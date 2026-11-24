# obs-blue &nbsp; [![bluebuild build badge](https://github.com/chasekidder/obs-blue/actions/workflows/build.yml/badge.svg)](https://github.com/chasekidder/obs-blue/actions/workflows/build.yml)

A dedicated bootc image for running OBS and allowing stream automation.

## Installation

> [!WARNING]  
> [This is an experimental feature](https://www.fedoraproject.org/wiki/Changes/OstreeNativeContainerStable), try at your own discretion.

To rebase an existing atomic Fedora installation to the tested build (`:stable`):

- First rebase to the unsigned image, to get the proper signing keys and policies installed:
  ```
  rpm-ostree rebase ostree-unverified-registry:ghcr.io/chasekidder/obs-blue:stable
  ```
- Reboot to complete the rebase:
  ```
  systemctl reboot
  ```
- Then rebase to the signed image, like so:
  ```
  rpm-ostree rebase ostree-image-signed:docker://ghcr.io/chasekidder/obs-blue:stable
  ```
- Reboot again to complete the installation
  ```
  systemctl reboot
  ```

CI builds `:latest` on every push to `main` (and on demand: **Actions → bluebuild → Run workflow**, e.g. to pick up base-image updates). The `:stable` tag only moves when you promote a tested build (see [Promoting a build](#promoting-a-build)), and the stream PC follows `:stable`. Builds always use the Fedora version specified in `recipe.yml`, so you won't get accidentally updated to the next major version.

## Updates and rollback

- The PC **stages** updates in the background (`rpm-ostreed-automatic`, `AutomaticUpdatePolicy=stage`, on unmetered networks) and applies them on the **next boot**. The PC is only on for streams, so it checks once, 2 minutes after power-on, while you set up; the download runs at idle CPU/IO priority with a 1 GB memory cap so it can't disturb OBS. There is no 04:00 or daily check. It never reboots by itself, and Flatpak auto-updates are off.
- **Automatic rollback (greenboot):** after an update is applied, `greenboot-healthcheck.service` runs `/etc/greenboot/check/required.d/50-stream-pilot.sh`. That check fails unless `stream-pilot` leaves `STARTING` within 5 minutes of the `stream` user's login. A failed boot is retried (`GREENBOOT_MAX_BOOT_ATTEMPTS=3`), then GRUB falls back to the previous deployment and greenboot makes that permanent. greenboot (0.16, the Rust rewrite) reboots on a failed check on **any** boot, including the first boot after install: the first failure sets `boot_counter=3` and reboots. Without a staged update there is nothing to roll back to, so a broken first boot reboots a few times before greenboot asks for manual intervention. To stop that while you debug: `sudo systemctl mask greenboot-healthcheck.service && sudo grub2-editenv - unset boot_counter`, and unmask it afterwards.
- Check the result with `journalctl -b -u greenboot-healthcheck.service`, or look at the boot status line shown at SSH login.
- **Manual rollback** over SSH, if something is wrong with a running image: `sudo bootc rollback && sudo systemctl reboot` (as an admin user or root).

## Promoting a build

`:latest` is built by CI on every push to `main`. After you have tested a build, promote its digest to `:stable`:

1. Find the digest of the `:latest` build you tested:
   ```
   skopeo inspect docker://ghcr.io/chasekidder/obs-blue:latest | jq -r .Digest
   ```
   (or read it from the package page on GitHub).
2. In the GitHub repo, open **Actions → promote → Run workflow** and paste the digest (`sha256:...`) into the `digest` input.
3. The workflow copies that exact digest to `:stable` (all architectures) and signs it with the repo's cosign key. The PC picks it up at its next power-on (the update check 2 minutes after boot), stages it, and applies it on the boot after that.

## First run on the stream PC

Do this once, after the first boot from the ISO. After that, boots need no interaction. Everything is done as the `stream` user, over RustDesk or SSH where noted.

- [ ] **Facebook login:** the status key shows `LOG IN` until the stream-pilot Chromium profile is logged in. In the stream-pilot Chromium window, log in to Facebook once, including 2FA. The session lives only in `~/.local/share/stream-pilot/chromium/`.
- [ ] **Social Stream Ninja (SSN):** in the SSN extension, import the settings backup (`.data` file), then turn on **Enable remote API control** and **Send chat messages to API server**.
- [ ] **RustDesk:** it starts at login (`/etc/xdg/autostart/com.rustdesk.RustDesk.desktop`) and is pre-authorized for Plasma's remote-desktop portal, so nobody has to click Allow. A window rule keeps its window minimized, so to set a **permanent password** first untick "stream-pilot: RustDesk starts minimized" in System Settings → Window Management → Window Rules, open RustDesk, set the password (Settings → Security), close the window and tick the rule again. Then connect from another computer and check that both the picture and the mouse work.
- [ ] **Camera and microphone IDs:** the names can differ from the old Debian install, so take them from this image:
  ```
  pactl list short sources
  ls -l /dev/v4l/by-id/ /dev/v4l/by-path/
  ```
  Put them in `~/.config/stream-pilot/site.toml`: `[cameras.main] device`, `[cameras.jewelry] device` and `[audio] mic_device`. Then re-seed the OBS scenes with the new devices:
  ```
  systemctl --user stop obs && stream-pilot seed-obs --reset-scenes && systemctl --user start obs
  ```
- [ ] **Private test-Group dry run:** with a private test Group configured in `site.toml` (only you as a member), run everything up to, but not including, clicking `Go live`:
  ```
  systemctl --user stop stream-pilot; stream-pilot run --dry-run
  ```
  When it looks right, `systemctl --user start stream-pilot` (or reboot) to return to normal operation.

For the full test-Group walkthrough (screenshot recording, a complete test live, ending, the comment report, and the hardware checks), see the [Facebook live validation runbook](docs/superpowers/runbooks/facebook-live-validation.md).

Useful afterwards:

- `stream-pilot status` shows the current state.
- `stream-pilot press` presses the status key over SSH (goes live when it shows `GO LIVE`); `stream-pilot press --long` ends a live without powering off.
- `journalctl --user -u stream-pilot -f` follows the live log.
- If an update misbehaves and greenboot did not catch it, roll back over SSH with `sudo bootc rollback && sudo systemctl reboot` (as an admin user or root).

## ISO

If build on Fedora Atomic, you can generate an offline ISO with the instructions available [here](https://blue-build.org/how-to/generate-iso/#_top). These ISOs cannot unfortunately be distributed on GitHub for free due to large sizes, so for public projects something else has to be used for hosting.

## Verification

These images are signed with [Sigstore](https://www.sigstore.dev/)'s [cosign](https://github.com/sigstore/cosign). You can verify the signature by downloading the `cosign.pub` file from this repo and running the following command:

```bash
cosign verify --key cosign.pub ghcr.io/chasekidder/obs-blue:stable
```
