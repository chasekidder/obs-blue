"""[deck_warnings]: MIC?, CAM?, NET and MEM on the spare key."""

import asyncio
import math

from stream_pilot.config import load_config
from stream_pilot.deck_warnings import FACES, DeckWarnings, NetWindow, peak_db
from stream_pilot.memory import BLANK_FACE, MEM_FACE, SPARE_KEY
from stream_pilot.states import State

from fakes import FakeDeck, FakeObs

MAIN_DEV = "/dev/v4l/by-id/main"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(tmp_path, extra="", *, devices=(MAIN_DEV,)):
    site = tmp_path / "site.toml"
    site.write_text(f'[cameras.main]\ndevice = "{MAIN_DEV}"\n[deck_warnings]\nenabled = true\n{extra}')
    cfg = load_config([site])
    obs, deck, clock = FakeObs(), FakeDeck(), Clock()
    obs.model.inputs["Logitech Camera 1 (Main)"]["settings"]["device_id"] = MAIN_DEV
    w = DeckWarnings(cfg, obs=obs, deck=deck, exists=lambda p: p in devices, clock=clock)
    return w, obs, deck, clock


def meters(db):
    level = 10 ** (db / 20)
    return {"inputs": [{"inputName": "Microphone", "inputLevelsMul": [[level, level, level]]}]}


def label(deck):
    return deck.faces[SPARE_KEY].label


# ------------------------------------------------------------------ pure


def test_peak_db():
    assert peak_db([[0.1, 0.5, 0.6]]) == 20 * math.log10(0.5)
    assert peak_db([[0, 0, 0]]) == -math.inf
    assert peak_db(None) == -math.inf


def burst_status(window, seconds, *, bad_from, bad_to, per_s=30, skip_per_s=7.5):
    skipped = total = 0
    for t in range(0, seconds, 2):
        total += 2 * per_s
        if bad_from <= t < bad_to:
            skipped += int(2 * skip_per_s)
        window.add(float(t), {"outputActive": True, "outputSkippedFrames": skipped,
                              "outputTotalFrames": total, "outputCongestion": 0.0})


def test_a_single_10_s_burst_does_not_trip_net():
    # measured on the stream PC: one 10 s congestion burst dropped 75 frames
    w = NetWindow(20, 1.0, 0.3)
    skipped = total = 0
    for t in range(0, 60, 2):
        total += 60
        if 20 <= t < 30:
            skipped += 15  # 75 frames over the 10 s
        w.add(float(t), {"outputActive": True, "outputSkippedFrames": skipped,
                         "outputTotalFrames": total, "outputCongestion": 0.0})
        assert not w.warn(), f"tripped at t={t}"


def test_a_sustained_problem_trips_net_and_clears_after():
    w = NetWindow(20, 1.0, 0.3)
    burst_status(w, 40, bad_from=10, bad_to=40)
    assert w.warn()
    w2 = NetWindow(20, 1.0, 0.3)
    burst_status(w2, 80, bad_from=10, bad_to=40)
    assert not w2.warn()


def test_sustained_congestion_alone_trips_net():
    w = NetWindow(20, 1.0, 0.3)
    total = 0
    for t in range(0, 30, 2):
        total += 60
        w.add(float(t), {"outputActive": True, "outputSkippedFrames": 0,
                         "outputTotalFrames": total, "outputCongestion": 0.6})
    assert w.warn()


# ------------------------------------------------------------------ MIC?


async def test_mic_silent_while_live(tmp_path):
    w, obs, deck, clock = make(tmp_path)
    await w.obs_connected()
    w.state_changed(State.LIVE)
    w.on_meters(meters(-30))
    clock.t += 31
    w.on_meters(meters(-90))
    w.render()
    assert label(deck) == "MIC?"
    clock.t += 1
    w.on_meters(meters(-20))
    w.render()
    assert deck.faces[SPARE_KEY] == BLANK_FACE


async def test_mic_off_the_air_before_go_live_is_not_a_warning(tmp_path):
    w, obs, deck, clock = make(tmp_path)
    await w.obs_connected()
    w.state_changed(State.READY)
    clock.t += 60  # no meters at all: a holding scene keeps the microphone off the air
    w.render()
    assert deck.faces[SPARE_KEY] == BLANK_FACE
    w.on_meters(meters(-95))  # but a reported, silent microphone is
    w.render()
    assert label(deck) == "MIC?"


async def test_meters_are_throttled(tmp_path):
    w, obs, deck, clock = make(tmp_path)
    await w.obs_connected()
    w.state_changed(State.LIVE)
    w.on_meters(meters(-90))
    sound_at = w._sound_at
    w.on_meters(meters(-10))  # within METER_EVERY_S: not looked at
    assert w._sound_at == sound_at


# ------------------------------------------------------------------ CAM?


async def test_missing_camera_device(tmp_path):
    w, obs, deck, clock = make(tmp_path, devices=())
    w.state_changed(State.READY)
    await w.step()
    assert label(deck) == "CAM?"


async def test_obs_source_on_a_missing_device(tmp_path):
    w, obs, deck, clock = make(tmp_path)
    obs.model.inputs["Logitech Camera 1 (Main)"]["settings"]["device_id"] = "/dev/video0"
    w.state_changed(State.READY)
    await w.step()
    assert label(deck) == "CAM?"


async def test_on_air_camera_source_inactive(tmp_path):
    w, obs, deck, clock = make(tmp_path)
    w.state_changed(State.LIVE)
    w._sound_at = clock.t + 1000
    await w.step()
    assert deck.faces[SPARE_KEY] == BLANK_FACE
    obs.model.input_active["Logitech Camera 1 (Main)"] = False
    clock.t += 11
    await w.step()
    assert label(deck) == "CAM?"


async def test_unlisted_cameras_are_not_checked(tmp_path):
    # the close-up camera's device is commented out on the PC: not listed, not checked
    w, obs, deck, clock = make(tmp_path)
    w.state_changed(State.READY)
    await w.step()
    assert deck.faces[SPARE_KEY] == BLANK_FACE


# ------------------------------------------------------------------ priority and MEM


async def test_priority_mic_cam_net_mem(tmp_path):
    w, obs, deck, clock = make(tmp_path)
    await w.obs_connected()
    w.state_changed(State.LIVE)
    w.show(SPARE_KEY, MEM_FACE)
    assert label(deck) == "MEM"
    for t in range(0, 22, 2):
        obs.model.stream_status.update(outputActive=True, outputTotalFrames=60 * (t + 1),
                                       outputSkippedFrames=10 * (t + 1))
        w.net.add(clock.t + t, dict(obs.model.stream_status))
    w.render()
    assert label(deck) == "NET"
    w.cam = True
    w.render()
    assert label(deck) == "CAM?"
    clock.t += 31
    w.on_meters(meters(-99))
    w.render()
    assert label(deck) == "MIC?"


async def test_outside_ready_and_live_only_mem_shows(tmp_path):
    w, obs, deck, clock = make(tmp_path, devices=())
    w.state_changed(State.SETUP)
    await w.step()
    assert deck.faces[SPARE_KEY] == BLANK_FACE
    w.show(SPARE_KEY, MEM_FACE)
    assert label(deck) == "MEM"
    w.show(1, FACES["NET"])  # other keys pass straight through
    assert deck.faces[1] == FACES["NET"]


async def test_a_broken_obs_never_raises_from_the_loop(tmp_path):
    w, obs, deck, clock = make(tmp_path)
    obs.request_raises["GetStreamStatus"] = ConnectionError("gone")
    obs.request_raises["GetCurrentProgramScene"] = ConnectionError("gone")
    w.state_changed(State.LIVE)
    slept = []

    async def sleep(s):
        slept.append(s)
        if len(slept) >= 3:
            raise asyncio.CancelledError

    w._sleep = sleep
    try:
        await w.run()
    except asyncio.CancelledError:
        pass
    assert len(slept) == 3


# ------------------------------------------------------------------ off


def test_off_registers_nothing(tmp_path):
    cfg = load_config([])
    obs = FakeObs()
    w = DeckWarnings(cfg, obs=obs, deck=FakeDeck())
    assert not w.enabled
    assert obs.event_handlers == {}


def test_app_wires_the_memory_guard_to_the_deck_when_off(monkeypatch, tmp_path):
    from stream_pilot import app

    monkeypatch.setenv("HOME", str(tmp_path))
    seen = {}

    class Guard:
        def __init__(self, cfg, deck, on_sample):
            seen["deck"] = deck

        async def run(self):
            pass

    monkeypatch.setattr(app, "MemoryGuard", Guard)
    cfg = load_config([])
    controller, background = app.build_app(cfg, dry_run=True, facebook=object(), capture=object())
    for aw in background:
        aw.close()
    assert seen["deck"] is controller.deck

    site = tmp_path / "site.toml"
    site.write_text("[deck_warnings]\nenabled = true\n")
    controller, background = app.build_app(load_config([site]), dry_run=True,
                                           facebook=object(), capture=object())
    for aw in background:
        aw.close()
    assert isinstance(seen["deck"], DeckWarnings)
    assert len(background) == 6
