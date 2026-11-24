# Phase 6: white-balance freeze and microphone filters

Date: 2026-11-24
Branch: `improve/6-camera-audio` (base: `improve/5-branding`)
Switches: `[wb_freeze] enabled` (default `false`; `camera` "jewelry", `freeze_exposure` false, `rebalance_s` 2) and `[audio_filters] enabled` (default `false`; `limiter_db` -1.0)

## White-balance freeze

**Why:** the close-up camera's auto white balance follows whatever fills the picture, so colourful items shift the colours while they are shown.

**Phase 0 findings that decide the design** (C920, `docs/superpowers/notes/2026-11-24-hardware-checks.md`):

1. Turning auto off jumps to the camera's **stored** manual value; it does not hold the current one.
2. The temperature can only be written once auto is off (`Permission denied` while auto is on), so it cannot be pre-loaded.
3. The temperature reported under auto does not match the picture auto produces (auto reported 3088 K with R/B 2.0; manual 3088 K gives R/B 1.32), so copying the reported value, from this camera or from the main camera, gives a different colour.

So the fallback the plan allowed ("copy the main camera's reported temperature, if reliable") is not reliable either. The feature is implemented with the best available value, the close-up camera's own reported temperature written right after auto goes off, and **ships disabled**: a freeze shows the stored value for a moment and then a slightly different colour (on the PC: R/B 1.98 on auto, 1.74 frozen).

**Behavior with the switch on:**

- Listens to OBS's `CurrentProgramSceneChanged` (so scene changes in OBS count, not only deck presses). When the close-up camera's scene goes on air: read its reported temperature, auto white balance off, write the temperature (and with `freeze_exposure`: read the exposure time, manual exposure, write it). When the scene leaves the air: auto white balance on (and exposure back to aperture priority).
- On every OBS connect the current program scene is applied; at startup auto is restored first.
- Control names are read from the device (`v4l2-ctl -l`): `white_balance_automatic` / `white_balance_temperature_auto`, `auto_exposure` / `exposure_auto`, `exposure_time_absolute` / `exposure_absolute`.
- `WbFreeze.rebalance()` and `stream-pilot rebalance [--camera NAME]`: auto on for `rebalance_s`, then frozen again (a camera on auto is left alone). No deck key.
- Every camera call is one bounded `v4l2-ctl`; a scene change is applied in its own task with a 10 s bound; an unplugged camera is not queried at all.

**Off:** no listener. The restore (startup and OBS connects) turns the camera's auto white balance on, and its exposure back to aperture priority if it is manual.

**Tested:** scene switching, ordering, exposure, re-balance, startup and off restore with a fake that behaves like the measured C920 (jumps to the stored value, refuses writes while auto is on). On the PC with the one connected camera (the main C920), driven directly: freeze, leave, freeze again, re-balance, exposure freeze, restore, and `stream-pilot rebalance --camera main` on auto and frozen. **Unverified:** the end-to-end close-up path (needs the second camera plugged in and configured).

## Microphone filters

- On the microphone input (the PulseAudio input whose device is `[audio] mic_device`, else "Microphone", else the first PulseAudio input): "Noise Suppression (stream-pilot)" (`noise_suppress_filter_v2`, RNNoise) then "Limiter (stream-pilot)" (`limiter_filter`, threshold `limiter_db`, release 60 ms), created over obs-websocket if missing, enabled on every OBS connect.
- Off: disabled if present, never created.
- Samples recorded on the PC through OBS (30 s each, room noise only, nobody speaking): `~/spi-samples/mic-filters-on.m4a` (mean −61.5 dB, max −41.6 dB) and `~/spi-samples/mic-filters-off.m4a` (mean −49.4 dB, max −21.7 dB). Left on the PC for a listening test.

## Image

Smoke test: `obs-filters.so` with RNNoise, `v4l2-ctl`, `stream-pilot rebalance --help`.

## Rollback

No new states or fields. An older image leaves the filters in OBS as they were last set.
