"""[branding]: watermark, outro card before StopStream, the live's title."""

import asyncio
import dataclasses
from datetime import datetime

from stream_pilot.branding import OUTRO_SCENE, WATERMARK, Branding, live_title, watermark_transform
from stream_pilot.events import DeckPress, ObsConnected, PowerPress
from stream_pilot.holding import STUDIO_SCENE, Holding
from stream_pilot.states import STATUS_FACES, State

from test_controller import Rig, make  # noqa: F401 (the rig fixture)

MAIN, JEWEL = "Main Camera", "Jewlery Camera"
NOW = datetime(2026, 11, 24, 15, 0)


class Sleeps:
    def __init__(self, rig):
        self.rig = rig
        self.calls = []

    async def __call__(self, s):
        self.calls.append((s, self.rig.deck.faces[0].label, self.rig.obs.model.program))
        await asyncio.sleep(0)


def branding(rig, sleeps=None):
    return [Branding(rig.cfg, obs=rig.obs, fb=rig.fb, deck=rig.deck,
                     sleep=sleeps or Sleeps(rig), now=lambda: NOW)]


def on(extra=""):
    return f'\n[branding]\nenabled = true\nwatermark_text = "Gem Shop"\noutro_s = 3\n{extra}'


# ------------------------------------------------------------------- pure


def test_live_title_default_template():
    assert live_title("{group_name} Sale – {date:%b %-d}", "Gem Shop", NOW) == "Gem Shop Sale – Nov 24"


def test_live_title_with_a_broken_template_is_skipped(caplog):
    assert live_title("{groupname} Sale", "Gem Shop", NOW) is None
    assert live_title("{date:%b", "Gem Shop", NOW) is None
    assert "cannot fill live_title_template" in caplog.text


def test_watermark_corners():
    assert watermark_transform("top-right") == {"positionX": 1040.0, "positionY": 40.0, "alignment": 6}
    assert watermark_transform("bottom-left") == {"positionX": 40.0, "positionY": 1880.0, "alignment": 9}


# ------------------------------------------------------------------- switch off


async def test_off_no_watermark_no_outro_no_title(make):
    rig = make(features=branding, shared=True)
    await rig.to_live()
    await rig.c.handle(ObsConnected())
    await rig.settle()
    m = rig.obs.model
    assert WATERMARK not in m.inputs and OUTRO_SCENE not in m.scenes
    assert "fb.set_title" not in rig.names(rig.fb)
    rig.clear()
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert rig.shared[0] == ("obs.stop_stream",)  # straight to StopStream, as today
    assert m.program == MAIN
    await rig.c.aclose()


async def test_off_hides_what_an_earlier_run_created(make):
    rig = make(features=branding)
    was_on = dataclasses.replace(rig.cfg, branding=dataclasses.replace(rig.cfg.branding, enabled=True))
    await Branding(was_on, obs=rig.obs, fb=rig.fb, deck=rig.deck).obs_connected()
    rig.obs.model.program = OUTRO_SCENE
    rig.obs.scene = OUTRO_SCENE
    await rig.c.handle(ObsConnected())
    await rig.settle()
    m = rig.obs.model
    assert m.item(MAIN, WATERMARK)["enabled"] is False
    assert m.item(JEWEL, WATERMARK)["enabled"] is False
    assert m.program == MAIN
    await rig.c.aclose()


# ------------------------------------------------------------------- switch on


async def test_watermark_in_every_camera_scene_one_source(make):
    rig = make(features=branding, site_extra=on('watermark_corner = "bottom-left"\n'))
    await rig.c.handle(ObsConnected())
    await rig.settle()
    m = rig.obs.model
    assert m.inputs[WATERMARK]["settings"]["text"] == "Gem Shop"
    for scene in (MAIN, JEWEL):
        item = m.item(scene, WATERMARK)
        assert item["enabled"] and item["transform"]["alignment"] == 9
        assert m.scenes[scene][-1]["source"] == WATERMARK  # on top
    assert "Microphone" not in [i["source"] for i in m.scenes[OUTRO_SCENE]]
    await rig.c.aclose()


async def test_title_is_typed_before_every_pre_roll(make):
    rig = make(features=branding, site_extra=on(), shared=True)
    await rig.to_ready()
    title = ("fb.set_title", "Test Group Sale – Nov 24")
    assert rig.shared.index(title) < rig.shared.index(("obs.start_stream",))
    await rig.c.aclose()


async def test_a_failing_title_never_blocks_ready(make):
    from stream_pilot.ports import StepFailed

    rig = make(features=branding, site_extra=on())
    rig.fb.set_title_raises = StepFailed("title", "not found")
    await rig.to_live()
    assert rig.c.state == State.LIVE
    await rig.c.aclose()


async def test_power_press_shows_the_outro_with_a_countdown_then_stops(make):
    sleeps = []

    def build(rig):
        s = Sleeps(rig)
        sleeps.append(s)
        return branding(rig, s)

    rig = make(features=build, site_extra=on(), shared=True)
    await rig.to_live()
    await rig.c.handle(DeckPress(5))  # the close-up camera is on air
    rig.clear()
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert sleeps[0].calls == [(1.0, "OUTRO\n3", OUTRO_SCENE), (1.0, "OUTRO\n2", OUTRO_SCENE),
                               (1.0, "OUTRO\n1", OUTRO_SCENE)]
    assert rig.shared[0] == ("obs.stop_stream",)  # StopStream came after the outro
    assert rig.obs.model.program == JEWEL  # what was on air is put back afterwards
    assert rig.deck.faces[0] == STATUS_FACES[State.ENDING]  # the countdown is gone again
    assert ("system.poweroff",) in rig.system.calls
    await rig.c.aclose()


async def test_long_press_gets_the_same_outro(make):
    rig = make(features=branding, site_extra=on())
    await rig.to_live()
    sleeps = rig.c.features[0]._sleep
    await rig.c.handle(DeckPress(0, long=True))
    await rig.settle()
    assert [c[2] for c in sleeps.calls] == [OUTRO_SCENE] * 3
    assert rig.c.state == State.READY and rig.obs.model.program == MAIN
    await rig.c.aclose()


async def test_no_outro_when_the_stream_is_already_down(make):
    rig = make(features=branding, site_extra=on())
    await rig.to_live()
    rig.c._transition(State.RECONNECTING)
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert rig.c.features[0]._sleep.calls == []
    await rig.c.aclose()


async def test_outro_is_capped(make):
    rig = make(features=branding, site_extra=on().replace("outro_s = 3", "outro_s = 600"))
    assert rig.c.features[0].outro_s == 15.0
    await rig.c.aclose()


async def test_a_hung_outro_still_stops_the_stream(make, monkeypatch):
    from stream_pilot import controller as controller_mod

    monkeypatch.setattr(controller_mod, "END_OUTRO_CAP_S", 0.1)

    async def hang(_s):
        await asyncio.Event().wait()

    rig = make(features=lambda rig: branding(rig, hang), site_extra=on())
    await rig.to_live()
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert ("obs.stop_stream",) in rig.obs.calls
    assert ("system.poweroff",) in rig.system.calls
    await rig.c.aclose()


async def test_with_holding_the_next_pre_roll_holds_after_the_outro(make):
    def build(rig):
        return [Holding(rig.cfg, obs=rig.obs, sleep=lambda s: asyncio.sleep(0)),
                *branding(rig)]

    rig = make(features=build, site_extra=on() + '\n[holding]\nenabled = true\n')
    await rig.to_live()
    await rig.c.handle(DeckPress(0, long=True))
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.obs.model.program == STUDIO_SCENE
    await rig.c.aclose()
