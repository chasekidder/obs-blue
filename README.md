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

- [ ] **Facebook login:** the status key shows `LOG IN` until the stream-pilot Chromium profile is logged in. In the stream-pilot Chromium window (the right-hand column), log in to Facebook once, including 2FA. The session lives only in `~/.local/share/stream-pilot/chromium/`.
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

### Optional features

Extra features each have their own section in `~/.config/stream-pilot/site.toml` with an `enabled` switch. They all ship **off**, and off means the PC behaves exactly as without the feature. `site.example/site.toml` lists every switch with its options, commented out. To turn one on, copy its section into `site.toml`, set `enabled = true`, and restart the controller (`systemctl --user restart stream-pilot`); switches are only read at startup. `stream-pilot status` lists each feature as `on` or `off`.

- [ ] **Recording (`[recording]`, optional):** records the microphone while live into `~/streams/<stream>/audio/` (30 s Ogg Opus clips listed in `audio/manifest.jsonl`) and writes `stream.json` and, last, `DONE` into the stream folder. Before turning it on, check that `[audio] mic_device` is the microphone (`pactl list short sources`); during a test live, `systemctl --user list-units 'stream-pilot-recorder@*'` shows the recorder running.
- [ ] **Upload (`[upload]`, optional):** copies the stream folders off the PC with rsync over SSH.
  1. On the PC: `stream-pilot upload-key` prints the PC's public key (created on first boot in `~/.config/stream-pilot/upload_ed25519`) and a suggested `authorized_keys` line.
  2. On the server: create a receiving account and an inbox directory, install `rrsync` (it comes with rsync), and add the printed line to the account's `~/.ssh/authorized_keys`, with your inbox directory in it. `restrict` plus `rrsync -wo` means the key can only write into that directory.
  3. In `site.toml`: `[upload] enabled = true`, `target = "<account>@<server>:<path inside the inbox>"`, and `host_key` from `ssh-keyscan -t ed25519 <server>` (the `ssh-ed25519 AAAA...` part). Restart the controller.
  4. Check: `journalctl --user -u stream-pilot-upload` shows a run about every minute, and `_status/status.json` appears on the server.
- [ ] **Holding scene (`[holding]`, optional):** before Go Live, OBS airs a "Starting soon" scene without the microphone (Studio Mode by default, the camera in the preview), and switches to the camera when Facebook confirms the live. With it on, the camera keys before Go Live change OBS's preview, not what is on air. Check once after turning it on: OBS shows "Preview: Main Camera" beside "Program: Holding" while the key is green, and the camera is on air (Studio Mode off) once the key turns red. A replay then starts with a few seconds of the card.
- [ ] **Branding (`[branding]`, optional):** a "Your logo here" text watermark in the camera scenes ("Branding Watermark" in OBS; change its text there or in `site.toml`), an "Outro" card for 8 s before the stream stops at the end of a live (the status key counts down `OUTRO 8` … `OUTRO 1`), and the live's title (`[facebook] live_title_template`, default "<group name> Sale – Nov 24") typed into the producer's post details before every pre-roll. Check once: the title shows under "Add post details" on the producer page while the key is green.
- [ ] **Mic filters (`[audio_filters]`, optional):** OBS noise suppression and a -1 dB limiter on the microphone. After turning it on, OBS shows "Noise Suppression (stream-pilot)" and "Limiter (stream-pilot)" in the Microphone's filters. Listen to a test recording before the first real stream.
- [ ] **White-balance freeze (`[wb_freeze]`, optional, not recommended yet):** freezes the close-up camera's white balance while its scene is on air. On the C920 a freeze shifts the colour slightly (see the hardware notes). Needs the close-up camera's `device` in `[cameras.jewelry]`. `stream-pilot rebalance` re-balances it while frozen.
- [ ] **Claim highlighting (`[claim_highlight]`, optional):** comments that look like purchase claims ("21 cookie sheets 3") get a yellow background in the comments column. Nothing else changes, including what is recorded.
- [ ] **Deck warnings (`[deck_warnings]`, optional):** the spare key (bottom middle) shows `MIC?`, `CAM?`, `NET` or `MEM` when something needs a look, before and during the live. They never block going live. Once the close-up camera is connected, add `"jewelry"` to `[deck_warnings] cameras`.
- [ ] **Diagnostics (`[diagnostics]`, optional, for the first streams):** a separate collector (`stream-pilot-diag@.service`, idle CPU and I/O priority, at most 15 % of one core and 256 MB) records every live into `~/streams/<stream>/diag/`: OBS frame counters every 2 s (`obs.jsonl`), network every 5 s (RTMP socket RTT and retransmits, Wi-Fi signal and retries, uplink, gateway ping: `network.jsonl`), CPU/memory per process, pressure, temperatures and clocks every 10 s (`system.jsonl`) and, with `gateway_status_url` set, the router's signal report every 60 s (`gateway.jsonl`). When the stream stops it adds both journals, the OBS log and `summary.txt` (frame-drop bursts with times, bitrate/RTT/Wi-Fi statistics, per-process CPU and memory growth, comment capture lag, comment column reloads). Stream keys are blanked; the folder can still hold people's names, so keep it private. Turn it off once the review is done.

Useful afterwards:

- `stream-pilot status` shows the current state and which optional features are on.
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
