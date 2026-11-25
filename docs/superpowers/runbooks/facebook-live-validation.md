# Facebook live validation runbook

Run this once on the real stream PC, before the first public stream. It proves that stream-pilot works against the **real Facebook UI** (everything so far was tested against fixtures) and that the image behaves on the real hardware. Use a private test Group, never the real one.

You need: the stream PC (screen + keyboard, or RustDesk/SSH), the Stream Deck, a Facebook account that can create a Group, and about 3 hours (the full run in part B should last about 2 hours).

Run all commands as the `stream` user on the stream PC.

> **Secrets warning.** Files under `~/fb-record/`, `~/.local/state/stream-pilot/failures/` and `~/streams/` can contain the **stream key**, group/actor IDs and people's names and comments. Never post them publicly or paste them into a chat or issue. Delete `~/fb-record/` when you are finished.

Where things live on the PC:

| What | Path |
|---|---|
| Site values (Facebook IDs, cameras, SSN) | `~/.config/stream-pilot/site.toml` |
| Image defaults | `/etc/stream-pilot/config.toml` (built-in defaults in the package, not edited by hand) |
| Controller state / status | `~/.local/state/stream-pilot/` (`state.json`, `status.json`) |
| Failure dumps | `~/.local/state/stream-pilot/failures/<timestamp>-<step>/` |
| Per-stream folder | `~/streams/<date>_<time>-<live id>/` with `comments.jsonl` and `report.txt` (with `[recording]` on also `audio/`, `stream.json` and `DONE`) |
| OBS logs | `~/.config/obs-studio/logs/` |
| Facebook UI texts and selectors | `stream-pilot/src/stream_pilot/fb/selectors.py` in the repo (the **only** file to change when Facebook looks different) |

Units (all user units): `obs.service`, `chromium.service`, `stream-pilot.service`.

---

## Part A. Test-Group dry run

### 1. Create a private test Group

1. In Facebook, create a new Group. Set privacy to **Private**. Do not invite anyone: only you are a member.
2. Name it something obvious, for example `stream-pilot test`.
3. Note the numeric Group ID (the number in `https://www.facebook.com/groups/<id>/`) and the Group's exact display name.

You should now have a Group where you are the only member. If you cannot see a numeric ID in the URL, open the Group's Producer page by hand (Go live, then Streaming software) and read `target_id=<TEST_GROUP_ID>` from the address bar.

### 2. Point `site.toml` at the test Group

1. Make a backup of the real values first, so you can restore them in step 10:
   ```
   cp ~/.config/stream-pilot/site.toml ~/site.toml.real
   ```
   The backup contains your real IDs and the SSN session ID. Keep it private.
2. Edit the file (`micro ~/.config/stream-pilot/site.toml`) and change only these two values:
   ```toml
   [facebook]
   group_id = "<TEST_GROUP_ID>"
   group_name = "<TEST_GROUP_NAME>"
   ```
   Leave `actor_id` (your personal profile ID) as it is.
3. Check that the file still parses:
   ```
   python3 -c "import tomllib,pathlib; tomllib.loads(pathlib.Path('~/.config/stream-pilot/site.toml').expanduser().read_text()); print('ok')"
   ```
   You should see `ok`. If you see a traceback, fix the quoting in the file.

`group_name` must match what Facebook shows exactly: stream-pilot uses it to confirm that the right Group is selected.

### 3. Dry run with recording

`--dry-run` runs the whole setup (login check, Go live, Streaming software, persistent key, key applied to OBS, OBS streams as a pre-roll, wait for Facebook's preview) and then stops in `READY`. When you press the status key, it only logs `dry-run: would go live` and does **not** click `Go live`. `--record DIR` additionally saves a screenshot and an accessibility snapshot of each producer setup step.

1. Make sure OBS and Chromium are running, then stop the controller so only your terminal copy runs:
   ```
   systemctl --user status obs chromium --no-pager
   systemctl --user stop stream-pilot
   ```
   You should see `obs.service` and `chromium.service` as `active (running)`. If not, `systemctl --user start obs chromium` and wait a minute.
2. Start the dry run:
   ```
   mkdir -p ~/fb-record
   stream-pilot run --dry-run --record ~/fb-record
   ```
3. Watch the terminal log and the Stream Deck status key. Expected path: `BOOT` -> `SETUP` (blue) -> `GO LIVE` (green, state `READY`). This normally takes a minute or two; the preview wait can take up to 3 minutes (`timeouts.preview_s = 180`).
4. When the key is green, press it once. The log should say `dry-run: would go live`. Nothing should go live on Facebook.
5. Stop the dry run with `Ctrl+C`.
6. Stop the OBS pre-roll stream if OBS is still streaming (press Stop Streaming in OBS). Then give control back to the service:
   ```
   systemctl --user start stream-pilot
   ```
   **Do not forget this step.** Without it nothing controls the PC.

If the key stays blue or turns orange, do not retry blindly: go to step 4. If it shows `LOG IN`, log in to Facebook in the stream-pilot Chromium window (this is the dedicated profile, not your normal browser) and run step 3 again.

### 4. Inspect the recording

1. List what was saved:
   ```
   ls ~/fb-record/
   ```
   You should see numbered pairs such as `01-<step>.png` and `01-<step>.yml`, one pair per producer setup step.
2. Open the PNG files in order (Dolphin, or copy them to your own computer over a private channel). For each, check that the page shows what the step name says.
3. Open the `.yml` files for the steps that look wrong. They are the accessibility tree that stream-pilot matches against (roles and names).
4. If a step failed, the controller saved a failure folder. It is listed in the log and lives under `~/.local/state/stream-pilot/failures/<timestamp>-<step>/`:
   ```
   ls ~/.local/state/stream-pilot/failures/
   ```
   The folder has `screenshot.png` (what Facebook rendered), `page.html` and `aria.yml`. Compare what Facebook shows with the texts in `fb/selectors.py`.
5. **The fix goes only in `stream-pilot/src/stream_pilot/fb/selectors.py`** (in the repo, then a normal build and promote). Do not patch other files for a Facebook UI difference. Send the recording (privately) to whoever maintains the repo if you cannot tell what to change.

Also check the following. They come from code review and have **not** been verified against real Facebook yet:

1. **Right element clicked.** Selector matching tries role elements first (buttons, links). If setup clicked the wrong `Go live` or `Streaming software` element (for example a menu entry or a heading), the recording shows it. Tighten the matching in `fb/selectors.py`.
2. **Required title** (check after the part B run; a dry run never opens the go-live dialog; with `[branding]` on, the title is set in the post details instead, see part B). If Facebook's go-live dialog requires a title, stream-pilot fills an empty required title textbox with `Live` and logs a WARNING. Check whether this happens:
   ```
   journalctl --user -u stream-pilot -b | grep -i title
   ```
   If you see the warning, decide whether a Group post titled `Live` is acceptable. If not, it needs a code change (not just a selector).
3. **Stream key and server read.** In the recording, the key and server fields must be readable (not masked). You should see OBS accept them: in OBS, Settings -> Stream shows the Custom service with a server ending in `/rtmp/`. Do not screenshot that page for sharing.

Pass criteria for part A: the status key reaches green `GO LIVE`, no failure folder was created, and the screenshots match the step names.

---

## Part B. Full test-Group run

### 5. Go live in the test Group (about 2 hours)

1. Make sure `site.toml` still points at the test Group, then start the service normally (a reboot is fine and also tests the boot path):
   ```
   systemctl --user start stream-pilot
   ```
2. Wait for the green `GO LIVE` key, then press it **once** (a short press; a press of 3 seconds or more is the "end" gesture). Over SSH, `stream-pilot press` does the same.
3. Check, within the first minute:
   - The key goes blinking blue, then red `LIVE` with the elapsed time.
   - The Facebook **comments column opens as a borderless column to the right of OBS**, on top of everything, with no title bar (KWin rule in `/etc/xdg/kwinrulesrc`). It is the live's dashboard showing only its Comments panel (heading "Comments", the comments, a "Write a comment…" box); seeing the dashboard's sidebar or widgets instead means the watcher did not pin the panel.
   - If the popout instead opens as a normal tab or window with a title bar, the `chromium --app=` handoff to the running browser failed and stream-pilot used its fallback (a CDP-opened window; comments are still captured). Confirm with `journalctl --user -u stream-pilot -b | grep -i "falling back"`. The handoff could not be tested in the headless test container, so this is a real hardware check; report it with the log line.
   - The OBS window fills the left part of the screen and does not overlap the popout.
   - In Facebook on your phone or another PC, the live is visible in the test Group.
4. Check the stream folder exists and `comments.jsonl` appears (it is created when the first comment is captured):
   ```
   ls -l ~/streams/
   ls -l ~/streams/*/
   ```
5. Post comments as yourself from your phone or another PC (you are the only member of the test Group), including:
   - a few normal comments,
   - the same word twice from the same person (for example `sold`, `sold`),
   - a reply to a comment,
   - an emoji-only comment.
6. Follow the capture while it runs:
   ```
   tail -f ~/streams/*/comments.jsonl
   ```
   Each line is JSON with `captured_at`, `source` (`fb_dom` or `ssn`), `author`, `text`, `live_id`, and `fb_comment_id` when available. You should see one `fb_dom` line and one `ssn` line per comment (while SSN is enabled).
7. Follow the controller log in a second terminal:
   ```
   journalctl --user -u stream-pilot -f
   ```
   Every minute it logs a one-line JSON memory sample.
8. Check that the **parked dashboard does not affect the stream**: the dashboard Chromium tab is moved to `about:blank` once live. The stream should stay live, the preview in Facebook should keep updating, and nothing should end on its own. If the live drops after parking, the fallback is to keep the dashboard open and reload it every 30 minutes (needs a code change).

Things to verify with the real comments during the 2-hour run (all unverified against real Facebook):

1. **Column text size.** The comments column is zoomed by `[facebook] comments_zoom` (default 1.5) and the comments fill the column's width. If the text is too small or too large, set `comments_zoom` in `site.toml` and restart stream-pilot; it applies when the column opens at the next Go Live. Do not use `Ctrl +` in the column: Chromium remembers that zoom per site, so it would also enlarge the producer page.
2. **Comment IDs.** Check whether lines carry a real `fb_comment_id`:
   ```
   grep -c '"fb_comment_id": *"[0-9]' ~/streams/*/comments.jsonl
   ```
   A count above zero means IDs are available from the popout page. If it is zero for `fb_dom`, IDs are not in the DOM; dedupe then relies on in-order alignment only (this is the designed fallback; check the report in step 8). (Seen on 2026-11-24: every `fb_dom` line had an ID.)
3. **Replies.** Facebook gives a reply a permalink containing `comment_id=<parent>&reply_comment_id=<child>`; stream-pilot uses the `reply_comment_id`, so a reply is its own line. Compare `comments.jsonl` with what the column shows: your test reply must appear as its own line. (Seen on 2026-11-24: it does.)
4. **Missed mid-list comments.** Comments inserted in the middle of the list (replies) after the initial load can be skipped by the DOM capture's realignment. SSN is the safety net in phase 1, and `report.txt` lists comments seen by only one capture.
5. **Real-time capture.** Chromium throttles timers in hidden or covered windows. The popout is always on top, and `chromium-launch` also passes the anti-throttling flags, so it should not be affected. Verify that lines arrive in real time: post a comment and compare the time you posted with `captured_at` (Unix seconds; `date -d @<captured_at>`). A lag of more than a few seconds, or lines arriving in bursts, is a failure.
6. **Known: the host's own comments** (posted from the stream-pilot Chromium) are captured twice: once in Facebook's "Posting..." state without an ID, then once posted (seen on 2026-11-24, with and without claim highlighting). Buyers' comments are not affected.
7. **Known limitation (not a bug to fix now).** If the popout is reloaded while several identical comments without IDs (for example `sold` from the same person) are visible, one real repeat can be dropped by the rescan. Note whether you see it; the SSN capture covers it in phase 1.

If `[recording]` is on in `site.toml` (see README, Optional features), also check during the live:

1. The recorder runs: `systemctl --user list-units 'stream-pilot-recorder@*'` shows one active unit.
2. Clips appear every 30 s: `cat ~/streams/*/audio/manifest.jsonl` has a `session` line and one `clip` line per finished clip. Play one on another computer (they are Ogg Opus; copy privately, they contain the room's audio).
3. `~/streams/<stream>/stream.json` has `live_id`, `preroll_started_at`, `go_live_pressed_at`, `live_confirmed_at`, `image` and `clock_at_live` (`ntp_synchronized` should be `true`).
4. Restart the controller mid-live (`systemctl --user restart stream-pilot`): the recorder keeps running (same unit, no new `session` line).

After the end (steps 6 and 7), the recorder unit is gone, the manifest ends with a `session_end` line, `stream.json` has `ended_at` and `clock_at_end`, and the folder has a `DONE` file.

If `[upload]` is on as well:

1. `systemctl --user list-timers stream-pilot-upload.timer` shows the timer, and `journalctl --user -u stream-pilot-upload -f` a run about every minute without `rsync exited` warnings.
2. On the server, during the live, `<target>/<stream folder>/audio/` fills up with finished clips and `<target>/_status/status.json` is rewritten every minute.
3. After the end, the server's copy has `journal.txt` and `DONE`, and the PC's folder has a `.uploaded` marker. If the server was unreachable at the end, the next boot (or the next timer run) finishes the copy.

If `[holding]` is on:

1. While the key is green, OBS shows "Program: Holding" (the "Starting soon" card) above "Preview: Main Camera" (studio mode), or "Starting Soon" with the camera and a banner (banner mode). The audio mixer's Microphone meter is marked "Preview" (not on air).
2. A camera key before Go Live changes the preview only. After Go Live, once the key is red, the chosen camera is on air and Studio Mode is off.
3. The replay starts with a few seconds of the card (Facebook starts the live about 3 s after the press, the camera goes on air about 8 s after it).

If `[branding]` is on:

1. While the key is green, the producer's "Add post details" shows the title (for example "Test Group Sale – Nov 24"); after Go Live the video in the test Group has that title.
2. The camera scenes show the watermark in the configured corner.
3. When you end the live (steps 6 and 7), the "Outro" card is on air for `outro_s` seconds and the status key counts down before OBS stops; afterwards OBS is back on the camera scene.

If `[audio_filters]` is on: in OBS, the Microphone's filters list "Noise Suppression (stream-pilot)" and "Limiter (stream-pilot)", both enabled. Speak loudly once: the meter should not reach 0 dB.

If `[wb_freeze]` is on (needs both cameras): switch to the close-up camera and back. `journalctl --user -u stream-pilot -f` shows "frozen at <n> K" when its scene goes on air and "back on auto" when it leaves, also when you switch scenes in OBS itself. `v4l2-ctl -d <close-up device> -C white_balance_automatic` reads 0 while frozen.

If `[claim_highlight]` is on: comments shaped like claims (`21 cookie sheets 3`, `$5 crew mens#31`) get a yellow background in the column; chat (`6 sure`, `Hi Penny`) does not. `comments.jsonl` has the same lines as without highlighting.

If `[deck_warnings]` is on: mute the Microphone in OBS for 35 s while live: the spare key shows `MIC?` (journal: `deck warning: MIC?`), and the warning clears after unmuting. Unplug the main camera briefly while the key is green: `CAM?` within 10 s.

### 6. End with a long-press

1. While live, **press and hold the status key for 3 seconds** (`deck.long_press_s = 3.0`), then release. Over SSH, `stream-pilot press --long` does the same.
2. Expected: the key shows red `ENDING`, OBS stops streaming, then the controller returns to setup (`SETUP`, then green `GO LIVE`) without powering off.
3. In Facebook (phone or other PC), check that the live **ended by itself**, with no manual "End live video". This works because Facebook's "end live when streaming stops" setting is on; stream-pilot waits 5 seconds (`fb_end_wait_s`) after OBS stops.
4. If the live is still running on Facebook, end it by hand in Facebook, and note it as a failure. Check the setup screenshot from step 4 for whether that setting was visible.

After a long-press end, the controller runs setup again (the same flow as part A), so the key goes blue and then green.

### 7. End with the power button

1. Press the Stream Deck key to go live again (green -> red `LIVE`). Wait a minute, post a comment.
2. Press the PC's **power button once** (a short press, not a hold).
3. Expected, in order: key `ENDING`, OBS stops, Facebook ends the live, the report is written, Chromium closes, OBS stops, then the PC powers off. It should take well under a minute.
4. If nothing happens, check that logind ignores the short press: `cat /usr/lib/systemd/logind.conf.d/90-stream-pilot.conf` should show `HandlePowerKey=ignore`, and `journalctl --user -u stream-pilot -b` should show the power press arriving. Even if stream-pilot is hung, `stream-pilot-power-guard.service` powers off cleanly 15 s after the press (up to 90 s more if stream-pilot is still ending the live); see `journalctl -u stream-pilot-power-guard -b`. Holding the button does nothing extra: this PC's button can't report a hold, and about 4 s triggers the firmware's hard power cut.
5. Power the PC back on and wait for autologin and a green key.

### 8. Check `report.txt`

1. Open the report for each test stream:
   ```
   ls ~/streams/
   cat ~/streams/<date>_<time>-<live id>/report.txt
   ```
2. You should see these lines: `comments by source` (`fb_dom` and `ssn` counts), `matched`, `only in ssn`, `only in fb_dom`, `gaps`, `corrupt lines`, then the lists of comments seen by only one capture.
3. What to look for:
   - `only in ssn` lists comments the DOM capture missed. Each one is a DOM capture bug or one of the known cases in step 5 (replies, mid-list inserts, reload with repeats). Note author, text and the situation.
   - `only in fb_dom` should be empty or explainable (SSN is a relay and can miss comments too).
   - `gaps` above 0 means a popout reload longer than the visible history; every comment was kept but some may be missing.
   - `corrupt lines` should be 0.
4. If `report.txt` does not exist, the stream folder has no `comments.jsonl`: capture never started. Check `journalctl --user -u stream-pilot -b | grep -iE 'capture|popout'`.

---

## Part C. Phase 2 and clean-up

### 9. Phase 2 criterion: remove Social Stream Ninja

After **3 consecutive real streams** where `report.txt` shows no DOM-only gaps versus SSN (nothing listed under `only in ssn`, so the DOM capture missed nothing SSN caught), SSN is no longer needed. Until then keep it on: it is the safety net.

1. In `~/.config/stream-pilot/site.toml`:
   ```toml
   [ssn]
   enabled = false
   ```
   (`[ssn] enabled` is the key defined in `config.py`; the package default is `true`. Check in `journalctl --user -u stream-pilot` after the restart that no SSN listener connects; if it still does, the flag is not wired up yet.) Restart: `systemctl --user restart stream-pilot`.
2. Remove the extension: delete its ID `cppibjhfemifednoimlblfcmjgfhfjeg` from `ExtensionInstallForcelist` in `files/system/etc/chromium/policies/managed/stream-pilot.json` in the repo (leave uBlock Origin Lite), build, test, promote as usual. Confirm the ID in `chrome://extensions` first (turn on Developer mode to see IDs).
3. Delete the SSN session ID from `site.toml` (`[ssn] session_id`) and from your own records, since it is a secret.
4. After the image update is applied, `comments.jsonl` should contain only `fb_dom` lines. Keep checking the first few reports.

### 10. Revert `site.toml` to the real Group

1. Restore the real values (keeps any other edits you made on purpose):
   ```
   micro ~/.config/stream-pilot/site.toml
   ```
   Set `[facebook] group_id` and `group_name` back to the real Group, or restore the backup if you changed nothing else: `cp ~/site.toml.real ~/.config/stream-pilot/site.toml`.
2. Remove the backup and the recordings:
   ```
   shred -u ~/site.toml.real
   rm -rf ~/fb-record
   ```
3. Restart the controller so it uses the real Group:
   ```
   systemctl --user restart stream-pilot
   ```
4. Check:
   ```
   stream-pilot status
   ```
   You should see `state:` move to `READY` once setup finishes, with `last error: -`. A green key means setup succeeded against the Group now in `site.toml`.
5. Delete the test Group in Facebook when you no longer need it.

---

## Hardware checks

These come from the spec's "Verify on hardware" table. Each row has a check and a fallback. Where the repo defines an expected value, it is stated; otherwise the line says "check that".

| # | Item | Check | Expected | If not |
|---|---|---|---|---|
| H1 | OBS RPM uses VAAPI H.264 | OBS log (see "Encoder and Stream Deck commands" below) | The profile uses `ffmpeg_vaapi_tex` (zero-copy VAAPI H.264, Level 4.1, CBR 4500, keyframe every 2 s). Check that the log shows a working VAAPI/libva device and no error about the encoder when streaming starts. | Try QuickSync; otherwise the pinned OBS Flatpak (not x264). |
| H2 | Pre-roll preview survives until `Go live` | Leave the PC at green `GO LIVE` for 10-20 minutes, then press | Check that the key is still green and the live starts. Facebook can mark the preview lost; stream-pilot then re-runs setup on its own (`journalctl --user -u stream-pilot -f`). | Start the OBS stream on button press instead (code change). |
| H3 | Parked dashboard does not affect live/auto-end | Part B steps 5-6 | The live stays up for 2 hours with the dashboard parked, and Facebook auto-ends on long-press. | Keep the dashboard open, reload it every 30 minutes. |
| H4 | SSN extension force-installs by policy | `chrome://policy` and `chrome://extensions` in the stream-pilot Chromium | Check that the policy `ExtensionInstallForcelist` shows two entries and both extensions are installed and enabled (SSN, uBlock Origin Lite). | Ship it unpacked and load it at startup. |
| H5 | Popout legible at a zoom that fits beside the portrait preview | Part B step 5, zoom item 1 | The KWin layout assumes 1920x1080: OBS 1344 px wide on the left, popout 576 px wide on the right. Check that comments are readable at some zoom. | Narrow the OBS area or shrink the preview scale. |
| H6 | Facebook comment IDs available | Part B step 5, comment IDs item | Check that `fb_comment_id` is filled. | In-order alignment dedupe only (already designed). |
| H7 | greenboot works | `journalctl -b -u greenboot-healthcheck` | After an update was applied, check that the health check passed. The check (`50-stream-pilot.sh`) runs `stream-pilot health --timeout 300` and needs `obs.service`, `chromium.service` and `stream-pilot.service` all active. On a first boot after install there is no rollback, only a status. | Manual rollback over SSH: `sudo bootc rollback && sudo systemctl reboot`. |
| H8 | RustDesk unattended access | Connect from another computer with the PC sitting at the login/stream screen, with no one at the PC | Check that you reach the Plasma screen without anyone clicking Allow, including after a reboot. | NanoKVM. |
| H9 | KWin rules and Plasma trims apply | `ls /etc/xdg/kwinrulesrc /etc/xdg/powerdevilrc`; look at the screen | OBS at the left (1344x1080, no title bar), popout on the right (576x1080, always on top, no title bar); no lock screen, no blanking or sleep after idle. Check that after a long idle there is no black screen. | First-boot seeding into `~/.config` (code change). |
| H10 | Autologin | Reboot and do nothing | `/etc/plasmalogin.conf.d/50-stream-autologin.conf` sets `User=stream`, `Session=plasma`. Check that the PC reaches the Plasma desktop by itself and a key turns green. | Configure whichever login manager ships (SDDM or Plasma Login Manager). |
| H11 | Updater is stage-only | `rpm-ostree status` and `cat /etc/rpm-ostreed.conf` | The config has `AutomaticUpdatePolicy=stage`. Check that a newer deployment shows as staged, and that the PC does not reboot by itself. | Disable the updater and update manually over SSH. |
| H12 | Stream Deck is the 6-key Mini | `lsusb` (see below) and look at the device | The code is written for a Stream Deck Mini (6 keys, 3x2). Check that the model matches. | Adjust the key layout in `deck.py` and the spec's table. |

### Encoder and Stream Deck commands

1. Encoder lines in the newest OBS log (start a stream first, for example during part A or B):
   ```
   LOG=~/.config/obs-studio/logs/$(ls -t ~/.config/obs-studio/logs/ | head -1)
   grep -iE 'vaapi|libva|encoder' "$LOG"
   ```
   Check that VAAPI initialises on the Intel device, that the encoder in use is `ffmpeg_vaapi_tex` (the id set in the profile's `[AdvOut] Encoder`), and that there is no fallback to x264 or an encoder error.
2. Stream Deck: `lsusb | grep -i elgato` lists the device (USB vendor `0fd9`). Check that it is the 6-key Mini and that the keys light up with the status key at top left.

### Memory and swap

1. Swappiness:
   ```
   cat /proc/sys/vm/swappiness
   ```
   Expected: `180` (set in `/usr/lib/sysctl.d/90-stream-pilot-zram.conf`, together with `vm.page-cluster = 0`).
2. zram swap is active:
   ```
   zramctl
   swapon --show
   ```
   Check that a `/dev/zram0` device exists with a non-zero size and is used as swap. If it is missing, the box swaps nowhere and memory pressure will hurt.
3. Unit memory settings (from `files/`):
   ```
   systemctl --user show chromium.service -p MemoryHigh -p OOMScoreAdjust
   systemctl --user show obs.service -p MemoryLow
   systemctl --user show stream-pilot.service -p MemoryHigh -p MemoryMax
   ```
   Expected: Chromium `MemoryHigh=5368709120` (5G) and `OOMScoreAdjust=500`; OBS `MemoryLow=1073741824` (1G). stream-pilot sets no limit, so `MemoryHigh=infinity` and `MemoryMax=infinity` are normal.
4. Who uses the memory (during the 2-hour run):
   ```
   systemd-cgls --user
   systemd-cgtop --user -m
   ```
   Check that OBS, the Chromium tree and stream-pilot are in separate units, and that Chromium stays below its 5G soft ceiling (it grows by about 4-5 MB a minute during a live, mostly Facebook's dashboard in the comments column; about 2.6 GB after 85 minutes on 2026-11-24).
5. systemd-oomd must not kill user units:
   ```
   systemctl --user show -p ManagedOOMMemoryPressure app.slice
   ```
   Expected: `auto` (the drop-in `90-stream-pilot-oomd.conf` turns the kill policy off). Check the same on `session.slice` if `app.slice` is not listed.
6. Available memory:
   ```
   stream-pilot status
   ```
   Expected output has the lines `state:`, `updated:`, `last error:`, `live id:` and `mem available: <n> MB`, then a `features:` block with one `<name>: on|off` line per optional feature (they match the `enabled` switches in `site.toml`). `updated` must be recent (a stale value means the controller is not running). The memory floor is `memory.floor_mb = 600`: check that `mem available` stays above it during the stream, and that the spare key never shows `MEM`.
7. The per-minute samples are in the journal:
   ```
   journalctl --user -u stream-pilot --since "1 hour ago" | grep mem_available_mb
   ```
   Check that there is a line each minute and that there are no `memory sampling failed` warnings.

---

## What to do with the results

- Everything passes: continue with one real test stream, then public streams, keeping the Debian disk image until three streams succeed (spec, Rollout step 5).
- A Facebook UI mismatch: fix `fb/selectors.py`, run the unit suite, rebuild, promote, apply and repeat part A.
- A hardware check fails: use its fallback from the table, and write the outcome into the spec's "Verify on hardware" table.
