# Phase 6 plan: white-balance freeze and microphone filters

Spec: `docs/superpowers/specs/2026-11-24-06-camera-audio.md`. Branch `improve/6-camera-audio`, based on `improve/5-branding`.

- [x] Decide the freeze from the phase 0 findings: the C920 jumps to its stored value and its reported value is not reliable, so implement with the camera's own reported value and ship disabled.
- [x] `camera_ctl.py`: parse `v4l2-ctl -l`, detect old/new control names, bounded get/set; an unplugged camera is not queried. Fixture: the PC's C920 control list.
- [x] `camera_features.py`: `WbFreeze` (scene-change events, freeze/unfreeze, exposure, rebalance, startup and off restore) and `AudioFilters` (mic lookup, RNNoise + limiter via the OBS items helper).
- [x] `[wb_freeze]`, `[audio_filters]` config; `stream-pilot rebalance`.
- [x] Tests: `test_camera_features.py` (a fake behaving like the measured C920).
- [x] PC: drive the main C920 with the feature (freeze, leave, rebalance, exposure, restore) and the subcommand; camera restored afterwards; filters on, samples on and off saved to `~/spi-samples/`; switch off disabled them; test filters removed.
- [x] Smoke test: `obs-filters.so` with RNNoise, `v4l2-ctl`, `stream-pilot rebalance --help`.
- [x] Docs: README, runbook, `site.example/site.toml`.
- [ ] End-to-end close-up freeze with the second camera (not connected).
