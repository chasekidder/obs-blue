"""[wb_freeze] and [audio_filters]."""

import asyncio
import dataclasses
from pathlib import Path

from stream_pilot.camera_ctl import Controls, V4l2Controls, detect, parse_controls
from stream_pilot.camera_features import SCENE_EVENT, AudioFilters, WbFreeze
from stream_pilot.events import ObsConnected
from stream_pilot.states import State

from test_controller import Rig, make  # noqa: F401 (the rig fixture)

DATA = Path(__file__).parent / "data"
MAIN, JEWEL = "Main Camera", "Jewlery Camera"
NEW = Controls("white_balance_automatic", "white_balance_temperature", "auto_exposure",
               "exposure_time_absolute")


class FakeC920:
    """Behaves like the C920 measured on 2026-11-24 (hardware notes)."""

    def __init__(self, names=NEW, *, present=True):
        self.device = "/dev/v4l/by-id/close-up"
        self._names = names if present else Controls()
        self.v = {names.wb_auto: 1, names.wb_temp: 3088, names.exposure_auto: 3,
                  names.exposure_abs: 333}
        self.stored_temp = 6500  # what the camera jumps to when auto goes off
        self.calls = []

    async def names(self):
        return self._names

    async def get(self, name):
        return self.v.get(name)

    async def set(self, **values):
        self.calls.append(values)
        for k, val in values.items():
            if k == self._names.wb_temp and self.v[self._names.wb_auto] == 1:
                return False  # Permission denied: inactive while auto is on
            if k == self._names.wb_auto and val == 0:
                self.v[self._names.wb_temp] = self.stored_temp
            if k == self._names.wb_temp:
                self.stored_temp = val
            self.v[k] = val
        return True


def freeze_feature(cam, **kw):
    def build(rig):
        return [WbFreeze(rig.cfg, obs=rig.obs, controls=cam, sleep=lambda s: asyncio.sleep(0), **kw)]
    return build


ON = "\n[wb_freeze]\nenabled = true\n"


# ---------------------------------------------------------------- controls


def test_parse_the_c920s_controls():
    ctrls = parse_controls((DATA / "v4l2-c920-ctrls.txt").read_text())
    assert ctrls["white_balance_temperature"]["flags"] == "inactive,"
    assert ctrls["white_balance_temperature"]["min"] == "2000"
    assert ctrls["auto_exposure"]["value"] == "3 (Aperture Priority Mode)"
    assert detect(ctrls) == NEW


def test_detect_older_kernel_names():
    text = ("white_balance_temperature_auto 0x0098090c (bool)   : default=1 value=1\n"
            "      white_balance_temperature 0x0098091a (int)    : min=2000 max=6500 value=4000\n"
            "                  exposure_auto 0x009a0901 (menu)   : min=0 max=3 default=3 value=3\n"
            "              exposure_absolute 0x009a0902 (int)    : min=3 max=2047 value=250\n")
    assert detect(parse_controls(text)) == Controls(
        "white_balance_temperature_auto", "white_balance_temperature", "exposure_auto",
        "exposure_absolute")


async def test_v4l2_controls_uses_v4l2_ctl():
    argvs = []

    async def exec_fn(*argv, **kw):
        argvs.append(argv)

        class P:
            returncode = 0

            async def communicate(self):
                return (b"white_balance_temperature: 3088\n", b"")

        return P()

    ctl = V4l2Controls("/dev/null", exec_fn=exec_fn)
    assert await ctl.get("white_balance_temperature") == 3088
    assert await ctl.set(white_balance_automatic=0, white_balance_temperature=3000)
    assert argvs == [
        ("v4l2-ctl", "-d", "/dev/null", "--get-ctrl=white_balance_temperature"),
        ("v4l2-ctl", "-d", "/dev/null",
         "--set-ctrl=white_balance_automatic=0,white_balance_temperature=3000"),
    ]


# ------------------------------------------------------------------ freeze


async def test_close_up_on_air_freezes_at_its_reported_value_and_leaving_restores_auto(make):
    cam = FakeC920()
    rig = make(features=freeze_feature(cam), site_extra=ON)
    f = rig.c.features[0]
    await rig.obs.emit(SCENE_EVENT, {"sceneName": JEWEL})
    await asyncio.gather(*f._tasks)
    # auto off first (the camera jumps to its stored 6500 K), then the reported value
    assert cam.calls[:2] == [{"white_balance_automatic": 0}, {"white_balance_temperature": 3088}]
    assert cam.v["white_balance_automatic"] == 0 and cam.v["white_balance_temperature"] == 3088
    await rig.obs.emit(SCENE_EVENT, {"sceneName": MAIN})
    await asyncio.gather(*f._tasks)
    assert cam.v["white_balance_automatic"] == 1
    await rig.c.aclose()


async def test_other_scene_changes_leave_the_camera_alone(make):
    cam = FakeC920()
    rig = make(features=freeze_feature(cam), site_extra=ON)
    f = rig.c.features[0]
    for scene in (MAIN, "Black", MAIN):
        await rig.obs.emit(SCENE_EVENT, {"sceneName": scene})
    await asyncio.gather(*f._tasks)
    assert cam.calls == []
    await rig.c.aclose()


async def test_obs_connect_applies_the_current_program(make):
    cam = FakeC920()
    rig = make(features=freeze_feature(cam), site_extra=ON, obs=None)
    rig.obs.model.program = rig.obs.scene = JEWEL
    await rig.c.handle(ObsConnected())
    await rig.settle()
    assert cam.v["white_balance_automatic"] == 0
    await rig.c.aclose()


async def test_startup_restores_auto(make):
    cam = FakeC920()
    cam.v["white_balance_automatic"] = 0
    cam.v["auto_exposure"] = 1
    rig = make(features=freeze_feature(cam), site_extra=ON)
    await rig.c.boot()
    await rig.settle()
    assert cam.v["white_balance_automatic"] == 1 and cam.v["auto_exposure"] == 3
    await rig.c.aclose()


async def test_freeze_exposure_too(make):
    cam = FakeC920()
    rig = make(features=freeze_feature(cam), site_extra=ON + "freeze_exposure = true\n")
    f = rig.c.features[0]
    await rig.obs.emit(SCENE_EVENT, {"sceneName": JEWEL})
    await asyncio.gather(*f._tasks)
    assert cam.v["auto_exposure"] == 1 and cam.v["exposure_time_absolute"] == 333
    await rig.obs.emit(SCENE_EVENT, {"sceneName": MAIN})
    await asyncio.gather(*f._tasks)
    assert cam.v["auto_exposure"] == 3
    await rig.c.aclose()


async def test_rebalance_runs_auto_then_freezes_again(make):
    cam = FakeC920()
    slept = []

    async def sleep(s):
        slept.append(s)
        cam.v["white_balance_temperature"] = 3300  # auto settled on a new value

    rig = make(features=lambda rig: [WbFreeze(rig.cfg, obs=rig.obs, controls=cam, sleep=sleep)],
               site_extra=ON)
    f = rig.c.features[0]
    await rig.obs.emit(SCENE_EVENT, {"sceneName": JEWEL})
    await asyncio.gather(*f._tasks)
    await f.rebalance()
    assert slept == [2]
    assert cam.v["white_balance_automatic"] == 0 and cam.v["white_balance_temperature"] == 3300
    await rig.c.aclose()


async def test_a_missing_camera_is_logged_not_raised(make, caplog):
    cam = FakeC920(present=False)
    rig = make(features=freeze_feature(cam), site_extra=ON)
    f = rig.c.features[0]
    await rig.obs.emit(SCENE_EVENT, {"sceneName": JEWEL})
    await asyncio.gather(*f._tasks, return_exceptions=True)
    assert "no white balance controls" in caplog.text
    await rig.to_live()
    assert rig.c.state == State.LIVE
    await rig.c.aclose()


# --------------------------------------------------------------- switch off


async def test_off_listens_to_nothing_and_restores_auto(make):
    cam = FakeC920()
    cam.v["white_balance_automatic"] = 0
    rig = make(features=freeze_feature(cam))
    assert SCENE_EVENT not in rig.obs.event_handlers
    await rig.c.boot()
    await rig.settle()
    assert cam.v["white_balance_automatic"] == 1
    assert cam.v["auto_exposure"] == 3  # untouched: it was not manual
    assert {"auto_exposure": 3} not in cam.calls
    await rig.c.aclose()


# ------------------------------------------------------------- audio filters


def filters(rig):
    return [AudioFilters(rig.cfg, obs=rig.obs)]


async def test_filters_on_the_microphone_noise_suppression_then_limiter(make):
    rig = make(features=filters, site_extra="\n[audio_filters]\nenabled = true\n")
    await rig.c.handle(ObsConnected())
    await rig.settle()
    f = rig.obs.model.filters["Microphone"]
    assert list(f) == ["Noise Suppression (stream-pilot)", "Limiter (stream-pilot)"]
    assert f["Noise Suppression (stream-pilot)"] == {
        "kind": "noise_suppress_filter_v2", "enabled": True, "settings": {"method": "rnnoise"}}
    assert f["Limiter (stream-pilot)"]["kind"] == "limiter_filter"
    assert f["Limiter (stream-pilot)"]["settings"]["threshold"] == -1.0
    await rig.c.aclose()


async def test_filters_find_the_mic_by_device(make):
    rig = make(features=filters, site_extra="\n[audio_filters]\nenabled = true\n")
    m = rig.obs.model
    m.inputs["Mic/Aux"] = {"kind": "pulse_input_capture",
                           "settings": {"device_id": rig.cfg.audio.mic_device}}
    await rig.c.handle(ObsConnected())
    await rig.settle()
    assert "Mic/Aux" in m.filters and "Microphone" not in m.filters
    await rig.c.aclose()


async def test_filters_off_creates_none_and_disables_existing_ones(make):
    rig = make(features=filters)
    await rig.c.handle(ObsConnected())
    await rig.settle()
    assert rig.obs.model.filters == {}
    on = dataclasses.replace(rig.cfg, audio_filters=dataclasses.replace(
        rig.cfg.audio_filters, enabled=True))
    await AudioFilters(on, obs=rig.obs).obs_connected()
    await rig.c.handle(ObsConnected())
    await rig.settle()
    assert all(not f["enabled"] for f in rig.obs.model.filters["Microphone"].values())
    await rig.c.aclose()


async def test_an_unplugged_camera_is_not_queried(tmp_path, caplog):
    argvs = []

    async def exec_fn(*argv, **kw):
        argvs.append(argv)
        raise AssertionError("must not run")

    ctl = V4l2Controls(str(tmp_path / "video2"), exec_fn=exec_fn)
    assert await ctl.names() == Controls()
    assert argvs == [] and "v4l2-ctl" not in caplog.text
