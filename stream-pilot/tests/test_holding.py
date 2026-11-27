"""[holding]: a scene without the microphone on air before Go Live."""

import asyncio
import dataclasses

from stream_pilot.events import DeckPress, EndRequested, ObsConnected
from stream_pilot.holding import BANNER_SCENE, STUDIO_SCENE, Holding
from stream_pilot.persist import Saved
from stream_pilot.states import State

from test_controller import Rig, make  # noqa: F401 (the rig fixture)

MAIN, JEWEL = "Main Camera", "Jewlery Camera"


async def no_sleep(_s):
    await asyncio.sleep(0)


def holding(rig):
    return [Holding(rig.cfg, obs=rig.obs, sleep=no_sleep)]


def on(mode="studio"):
    return f'\n[holding]\nenabled = true\nmode = "{mode}"\ntext = "Back in a minute"\n'


def sources(rig, scene):
    return [i["source"] for i in rig.obs.model.scenes[scene]]


# ------------------------------------------------------------------ switch off


async def test_off_changes_no_scene_before_go_live(make):
    rig = make(features=holding)
    await rig.to_ready()
    m = rig.obs.model
    assert m.program == MAIN and m.studio_mode is False
    assert STUDIO_SCENE not in m.scenes and BANNER_SCENE not in m.scenes
    await rig.c.handle(DeckPress(5))
    assert m.program == JEWEL  # camera keys switch the on-air scene, as today
    await rig.c.handle(DeckPress(0))
    await rig.settle()
    assert rig.c.state == State.LIVE and m.program == JEWEL
    await rig.c.aclose()


async def test_off_restores_a_holding_scene_left_on_air(make):
    rig = make(features=holding)
    m = rig.obs.model
    was_on = dataclasses.replace(rig.cfg, holding=dataclasses.replace(rig.cfg.holding, enabled=True))
    await Holding(was_on, obs=rig.obs, sleep=no_sleep).before_preroll()
    assert m.program == STUDIO_SCENE and m.studio_mode
    await rig.c.handle(ObsConnected())
    await rig.settle()
    assert m.program == MAIN and m.studio_mode is False
    assert all(not i["enabled"] for i in m.scenes[STUDIO_SCENE])
    assert STUDIO_SCENE in m.scenes  # scenes are never deleted
    assert m.item(MAIN, "Logitech Camera 1 (Main)")["enabled"] is True  # the camera scene untouched
    await rig.c.aclose()


# ------------------------------------------------------------------ studio mode


async def test_studio_holding_on_air_with_the_camera_in_preview_before_the_pre_roll(make):
    rig = make(features=holding, site_extra=on(), shared=True)
    rig.obs.hooks["SetStudioModeEnabled"] = lambda: rig.shared.append(("studio mode on",))
    await rig.to_ready()
    m = rig.obs.model
    assert m.studio_mode and m.program == STUDIO_SCENE and m.preview == MAIN
    assert "Microphone" not in sources(rig, STUDIO_SCENE)
    text = m.inputs["Holding Text"]
    assert text["kind"] == "text_ft2_source_v2" and text["settings"]["text"] == "Back in a minute"
    assert rig.shared.index(("studio mode on",)) < rig.shared.index(("obs.start_stream",))
    await rig.c.aclose()


async def test_studio_camera_keys_change_the_preview_not_the_air(make):
    rig = make(features=holding, site_extra=on())
    await rig.to_ready()
    m = rig.obs.model
    await rig.c.handle(DeckPress(5))
    assert m.program == STUDIO_SCENE and m.preview == JEWEL
    assert rig.deck.faces[5].bg == "#1f5fbf"  # the key still shows the chosen camera
    await rig.c.aclose()


async def test_studio_previewed_camera_goes_on_air_when_the_live_is_confirmed(make):
    rig = make(features=holding, site_extra=on())
    await rig.to_ready()
    m = rig.obs.model
    await rig.c.handle(DeckPress(5))
    rig.fb.gates["go_live"] = gate = asyncio.Event()
    await rig.c.handle(DeckPress(0))
    await rig.wait_state(State.GOING_LIVE)
    assert m.program == STUDIO_SCENE  # not on the key press
    gate.set()
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert m.program == JEWEL and m.studio_mode is False
    await rig.c.aclose()


async def test_studio_camera_keys_while_live_switch_the_air_and_never_hold(make):
    rig = make(features=holding, site_extra=on())
    await rig.to_live()
    m = rig.obs.model
    await rig.c.handle(DeckPress(5))
    assert m.program == JEWEL
    await rig.c.handle(DeckPress(2))
    assert m.program == MAIN and m.studio_mode is False
    await rig.c.aclose()


async def test_after_a_long_press_end_the_next_pre_roll_holds_again(make):
    rig = make(features=holding, site_extra=on())
    await rig.to_live()
    await rig.c.handle(EndRequested())
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.obs.model.program == STUDIO_SCENE and rig.obs.model.studio_mode
    await rig.c.aclose()


async def test_resume_in_live_puts_a_camera_scene_on_air(make):
    rig = make(features=holding, site_extra=on())
    m = rig.obs.model
    await Holding(rig.cfg, obs=rig.obs, sleep=no_sleep).before_preroll()
    rig.store.save(Saved("LIVE", "42", str(rig.cfg.paths.streams_dir / "x-42")))
    rig.obs.active = True
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert m.program == MAIN and m.studio_mode is False
    await rig.c.aclose()


# ------------------------------------------------------------------ banner mode


async def test_banner_shows_the_camera_and_the_text_without_the_microphone(make):
    rig = make(features=holding, site_extra=on("banner"))
    m = rig.obs.model
    m.item(MAIN, "Logitech Camera 1 (Main)")["transform"].update(
        {"positionX": 1080.0, "rotation": 90.0})
    await rig.to_ready()
    assert m.program == BANNER_SCENE and m.studio_mode is False
    assert sources(rig, BANNER_SCENE) == [
        "Logitech Camera 1 (Main)", "Starting Soon Bar", "Starting Soon Text"]
    cam = m.item(BANNER_SCENE, "Logitech Camera 1 (Main)")
    assert cam["transform"]["rotation"] == 90.0 and cam["transform"]["positionX"] == 1080.0
    await rig.c.handle(DeckPress(5))
    assert m.program == BANNER_SCENE
    await rig.c.handle(DeckPress(0))
    await rig.settle()
    assert m.program == JEWEL
    await rig.c.aclose()


# ------------------------------------------------------------------ robustness


async def test_a_broken_holding_never_blocks_ready_or_go_live(make):
    from stream_pilot.obs import ObsRequestFailed

    rig = make(features=holding, site_extra=on())
    for rt in ("SetStudioModeEnabled", "CreateScene", "SetCurrentProgramScene"):
        rig.obs.request_raises[rt] = ObsRequestFailed(rt, 500, "boom")
    await rig.to_live()
    assert rig.c.state == State.LIVE
    await rig.c.aclose()


async def test_host_changed_the_air_before_go_live_keys_work_as_today(make):
    rig = make(features=holding, site_extra=on())
    await rig.to_ready()
    rig.obs.scene = MAIN  # the host put the camera on air in OBS
    await rig.c.handle(DeckPress(5))
    assert rig.obs.scene == JEWEL
    await rig.c.aclose()


async def test_studio_setup_again_keeps_the_camera_in_preview(make):
    """Seen on the PC: OBS swaps preview and program after a transition, even to the same scene."""
    rig = make(features=holding, site_extra=on())
    await rig.to_ready()
    h = rig.c.features[0]
    await h.before_preroll()  # setup runs again (READY lost, OBS restarted, a new live)
    m = rig.obs.model
    assert m.program == STUDIO_SCENE and m.preview == MAIN
    await rig.c.aclose()


async def test_banner_mode_turns_off_studio_mode_left_from_studio_mode(make):
    rig = make(features=holding, site_extra=on("banner"))
    rig.obs.model.studio_mode = True
    await rig.to_ready()
    assert rig.obs.model.studio_mode is False and rig.obs.model.program == BANNER_SCENE
    await rig.c.aclose()


async def test_off_hides_the_banner_scene_including_its_camera_item(make):
    rig = make(features=holding)
    m = rig.obs.model
    was_on = dataclasses.replace(rig.cfg, holding=dataclasses.replace(
        rig.cfg.holding, enabled=True, mode="banner"))
    await Holding(was_on, obs=rig.obs, sleep=no_sleep).before_preroll()
    await rig.c.handle(ObsConnected())
    await rig.settle()
    assert all(not i["enabled"] for i in m.scenes[BANNER_SCENE])
    assert m.item(MAIN, "Logitech Camera 1 (Main)")["enabled"] is True
    assert m.program == MAIN
    await rig.c.aclose()
