# Hardware checks, 2026-11-24

Measured on the stream PC (image `44.20261124.1`, `:stable` promoted 20:23 UTC) for the feature phases. The room was dimly lit (evening, mean camera picture brightness about 15-20 of 255).

## Main camera (Logitech C920, `usb-046d_HD_Pro_Webcam_C920_54E96FEF`)

Method: controls set with `v4l2-ctl`; the picture's colour measured from OBS screenshots of the camera source (`GetSourceScreenshot`, 96 px wide, mean R/G/B), since OBS holds the device. "R/B" is mean red over mean blue. Higher `white_balance_temperature` values make the picture redder (the value tells the camera what colour the light is).

Controls: `white_balance_automatic` (default 1), `white_balance_temperature` 2000-6500, step 1, default 4000, flagged `inactive` while auto is on; `auto_exposure` 3 (aperture priority), `exposure_time_absolute` (inactive in aperture priority). These are the kernel's (uvcvideo) names on this image.

| Check | Result |
|---|---|
| (a) Auto white balance turned off: holds or jumps? | **Jumps to the stored manual value.** Stored 6500 K, auto on (R/B 1.95, normal picture), auto off: R/B 37 within 0.5 s, the same as manual 6500 K. The value reported after turning auto off is the stored one. |
| Can the value be written while auto is on (to pre-load it)? | **No.** `v4l2-ctl --set-ctrl=white_balance_temperature=...` while auto is on fails with `Permission denied` (the control is inactive). It can only be written after auto is off, so a freeze always shows the stored value for a moment first. |
| Is the temperature reported under auto the value the camera applies? | **No, not reliably.** Auto reported 3088 K with R/B 2.0; manual 3088 K gives R/B 1.32 (manual 2500 K: 0.55, 4000 K: 3.9). The auto picture matches roughly 3300 K, so copying the reported value gives a visibly different colour. |
| (b) Does the reported value follow the light with auto on? | Constant (3088 K) over 13 readings, 10 s apart, with unchanged light. It differed between auto engagements without a light change (2791 K before a reboot, 3088 K and 3033 K after). The light could not be changed remotely, so following a real lighting change is **unverified**. |
| (c) How long auto takes to settle after being turned back on | **At most 0.25 s** (one sample) from both 6500 K and 2000 K: the camera resumes its previous auto state instead of converging again. Under unchanged light that state is right; after a lighting change it may take longer (unverified). |

Afterwards: auto white balance back on, stored manual value left at the factory default 4000 K (the value stored before the test could not be read: under auto the camera reports its estimate). Exposure, focus and `exposure_dynamic_framerate` were not touched.

**Consequence for `[wb_freeze]` (phase 6):** freezing cannot simply turn auto off (the picture jumps to whatever was stored last), and the reported temperature is not a reliable value to copy, from this camera or the main one.

## Microphone in the OBS scene collection

`~/.config/obs-studio/basic/scenes/Untitled.json` on the PC: the microphone (`Microphone`, `pulse_input_capture`, device `alsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback`, sync offset +140 ms) is a **source inside each camera scene** ("Main Camera" and "Jewlery Camera"), not a global audio device: the collection has no `DesktopAudioDevice*` / `AuxAudioDevice*` entries. "Black" has no sources. So a scene without the microphone is silent; nothing has to be muted.

## Does Facebook's replay include the pre-roll?

Method: one test-Group live ("My Test Group"), with a temporary wall-clock text overlay on "Main Camera" (removed afterwards). Replay read in a temporary tab of the stream-pilot Chromium (`<video>.duration`, frames at 0 s and at the end).

| Event | Wall clock (EST) |
|---|---|
| OBS pre-roll stream started (OBS output duration) | 15:46:33.2 |
| Go Live pressed (`stream-pilot press`) | 15:55:36.7 |
| Live confirmed (`End live video` seen, state `LIVE`) | 15:55:44.7 |
| Long press (stream stopped) | 15:57:56.9 |

Replay (the test live): duration 137.97 s; first frame shows 15:55:39, last frame 15:57:57.

**The replay does not include the pre-roll.** It starts about 3 s after the Go Live press (when Facebook starts the live, before stream-pilot sees the confirmation) and ends when the stream stops. The 9 minutes of pre-roll are not in it. So `[recording] replay_includes_preroll = false`, and a replay starts with at most about 3 s of whatever was on air at the press.
