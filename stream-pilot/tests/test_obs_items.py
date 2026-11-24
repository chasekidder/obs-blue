import pytest

from stream_pilot.obs import ObsRequestFailed
from stream_pilot.obs_items import Filter, Scene, SceneItem, apply_items

from fakes import FakeObs

ITEMS = (
    Scene("Starting Soon"),
    SceneItem("Starting Soon", "Banner", kind="text_ft2_source_v2",
              settings={"text": "Starting soon"}, transform={"positionX": 10.0}),
    SceneItem("Starting Soon", "Logitech Camera 1 (Main)"),
    Filter("Microphone", "Limiter", "limiter_filter", {"threshold": -1.0}),
)


async def test_on_creates_missing_items_and_enables_them():
    obs = FakeObs()
    res = await apply_items(obs, ITEMS, True, owner="test")
    m = obs.model
    assert "Starting Soon" in m.scenes
    banner = m.item("Starting Soon", "Banner")
    assert banner["enabled"] is True
    assert banner["transform"] == {"positionX": 10.0}
    assert m.inputs["Banner"] == {"kind": "text_ft2_source_v2",
                                  "settings": {"text": "Starting soon"}}
    assert m.item("Starting Soon", "Logitech Camera 1 (Main)")["enabled"] is True
    assert m.filters["Microphone"]["Limiter"]["enabled"] is True
    assert res.failed == []
    assert len(res.created) == 4


async def test_on_is_idempotent_and_keeps_the_hosts_settings():
    obs = FakeObs()
    await apply_items(obs, ITEMS, True)
    m = obs.model
    m.inputs["Banner"]["settings"]["text"] = "Host's own text"
    m.item("Starting Soon", "Banner")["transform"]["positionX"] = 99.0
    m.filters["Microphone"]["Limiter"]["settings"]["threshold"] = -3.0
    m.item("Starting Soon", "Banner")["enabled"] = False
    obs.requests.clear()
    res = await apply_items(obs, ITEMS, True)
    assert res.created == []
    assert m.inputs["Banner"]["settings"]["text"] == "Host's own text"
    assert m.item("Starting Soon", "Banner")["transform"]["positionX"] == 99.0
    assert m.filters["Microphone"]["Limiter"]["settings"]["threshold"] == -3.0
    assert m.item("Starting Soon", "Banner")["enabled"] is True
    names = [r[0] for r in obs.requests]
    assert not {"CreateScene", "CreateInput", "CreateSceneItem", "CreateSourceFilter",
                "SetInputSettings", "SetSceneItemTransform"} & set(names)


async def test_off_creates_nothing():
    obs = FakeObs()
    res = await apply_items(obs, ITEMS, False)
    assert "Starting Soon" not in obs.model.scenes
    assert "Banner" not in obs.model.inputs
    assert obs.model.filters == {}
    assert res.created == [] and res.failed == []
    assert not [r for r in obs.requests if r[0].startswith(("Create", "Set"))]


async def test_off_disables_what_an_earlier_run_created_and_keeps_the_scene():
    obs = FakeObs()
    await apply_items(obs, ITEMS, True)
    res = await apply_items(obs, ITEMS, False)
    m = obs.model
    assert "Starting Soon" in m.scenes
    assert m.item("Starting Soon", "Banner")["enabled"] is False
    assert m.item("Starting Soon", "Logitech Camera 1 (Main)")["enabled"] is False
    assert m.filters["Microphone"]["Limiter"]["enabled"] is False
    assert len(res.disabled) == 3


async def test_existing_input_is_added_as_a_scene_item_not_recreated():
    obs = FakeObs()
    await apply_items(obs, [SceneItem("Black", "Microphone", kind="pulse_input_capture",
                                      settings={"device_id": "other"})], True)
    assert obs.model.inputs["Microphone"]["settings"] == {"device_id": "mic"}
    assert obs.model.item("Black", "Microphone")["enabled"] is True
    assert "CreateInput" not in [r[0] for r in obs.requests]


async def test_one_failing_item_does_not_stop_the_rest(caplog):
    obs = FakeObs()
    obs.request_raises["CreateSourceFilter"] = ObsRequestFailed("CreateSourceFilter", 605, "x")
    items = (Filter("Microphone", "Limiter", "limiter_filter"),
             Scene("Starting Soon"))
    res = await apply_items(obs, items, True, owner="branding")
    assert res.failed == ["filter 'Limiter' on 'Microphone'"]
    assert "Starting Soon" in obs.model.scenes
    assert "branding: cannot apply" in caplog.text


async def test_transport_failure_is_reported_not_raised():
    obs = FakeObs()
    obs.request_raises["GetSceneList"] = ConnectionError("gone")
    res = await apply_items(obs, [Scene("X")], True)
    assert res.failed == ["scene 'X'"]


async def test_cancellation_propagates():
    import asyncio

    obs = FakeObs()
    obs.gates["request"] = asyncio.Event()
    task = asyncio.ensure_future(apply_items(obs, [Scene("X")], True))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
