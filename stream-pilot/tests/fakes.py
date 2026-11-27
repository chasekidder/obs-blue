"""In-memory fakes for the controller ports.

Every fake records its calls in ``self.calls`` (a list of tuples). Pass a shared
list as ``calls=`` to several fakes to assert cross-object call order.

Configurable results use "script" values: a plain value is returned/raised on
every call; a list is consumed one entry per call and the last entry repeats.
For ``*_raises`` options ``None`` means "do not raise"; an exception instance is
raised.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from stream_pilot.ports import StepFailed, StreamTarget
from stream_pilot.states import KeyFace


class Script:
    def __init__(self, spec: Any):
        self._items = list(spec) if isinstance(spec, list) else [spec]
        self._i = 0

    def next(self) -> Any:
        item = self._items[min(self._i, len(self._items) - 1)]
        self._i += 1
        return item

    def raise_next(self) -> None:
        exc = self.next()
        if exc is not None:
            raise exc


class Gated:
    """Per-method hooks and gates for tests.

    ``self.hooks["name"] = fn`` runs ``fn()`` when the method is entered;
    ``self.gates["name"] = asyncio.Event()`` makes the method block until the
    event is set (never set it to simulate a hung adapter).
    """

    def _init_gated(self) -> None:
        self.hooks: dict[str, Any] = {}
        self.gates: dict[str, asyncio.Event] = {}

    async def _gated(self, name: str) -> None:
        hook = self.hooks.get(name)
        if hook is not None:
            hook()
        gate = self.gates.get(name)
        if gate is not None:
            await gate.wait()


class FakeObs(Gated):
    def __init__(
        self,
        *,
        active: bool = False,
        scene: str = "Main Camera",
        connect_raises: Any = None,
        start_raises: Any = None,
        stop_raises: Any = None,
        active_raises: Any = None,
        set_scene_raises: Any = None,
        stop_keeps_active: bool = False,
        calls: list | None = None,
    ):
        self._init_gated()
        self.calls: list[tuple] = calls if calls is not None else []
        self.active = active
        self.scene = scene
        self.stop_keeps_active = stop_keeps_active
        self.sink = None
        self._connect = Script(connect_raises)
        self._start = Script(start_raises)
        self._stop = Script(stop_raises)
        self._active = Script(active_raises)
        self._set_scene = Script(set_scene_raises)
        # Generic requests (``request``) are recorded here, not in ``calls``.
        self.requests: list[tuple[str, dict | None]] = []
        self.request_raises: dict[str, Exception] = {}
        self.connect_listeners: list = []
        self.event_handlers: dict[str, list] = {}
        self.model = ObsModel(program=scene)

    def set_event_sink(self, sink) -> None:
        self.calls.append(("obs.set_event_sink",))
        self.sink = sink

    async def connect(self) -> None:
        self.calls.append(("obs.connect",))
        self._connect.raise_next()

    async def stream_active(self) -> bool:
        self.calls.append(("obs.stream_active",))
        await self._gated("stream_active")
        self._active.raise_next()
        return self.active

    async def set_stream_target(self, target: StreamTarget) -> None:
        self.calls.append(("obs.set_stream_target", target))
        if self.active:
            # Real OBS: "500 You cannot change stream service settings while streaming."
            raise StepFailed("obs", "SetStreamServiceSettings: 500 streaming")

    async def start_stream(self) -> None:
        self.calls.append(("obs.start_stream",))
        self._start.raise_next()
        self.active = True
        hook = self.hooks.get("start_stream_done")
        if hook is not None:
            hook()

    async def stop_stream(self) -> None:
        self.calls.append(("obs.stop_stream",))
        await self._gated("stop_stream")
        self._stop.raise_next()
        if not self.stop_keeps_active:
            self.active = False

    async def set_scene(self, name: str) -> None:
        self.calls.append(("obs.set_scene", name))
        await self._gated("set_scene")
        self._set_scene.raise_next()
        self.scene = name
        self.model.program = name

    async def current_scene(self) -> str:
        self.calls.append(("obs.current_scene",))
        await self._gated("current_scene")
        return self.scene

    # -- generic requests (backed by self.model) ----------------------------

    def add_connect_listener(self, listener) -> None:
        self.connect_listeners.append(listener)

    def on_event(self, event: str, handler) -> None:
        self.event_handlers.setdefault(event, []).append(handler)

    async def emit(self, event: str, data: dict | None = None) -> None:
        for handler in self.event_handlers.get(event, []):
            result = handler(data)
            if asyncio.iscoroutine(result):
                await result

    async def request(self, request_type: str, data: dict | None = None) -> dict | None:
        self.requests.append((request_type, data))
        await self._gated("request")
        await self._gated(request_type)
        failing = self.request_raises.get(request_type)
        if failing is not None:
            raise failing
        self.model.program = self.scene
        out = self.model.handle(request_type, data or {})
        self.scene = self.model.program
        return out

class ObsModel:
    """The bits of an OBS instance the obs-websocket requests in stream-pilot touch.

    Starts like the seeded scene collection: ``Main Camera`` (Microphone + main camera),
    ``Jewlery Camera`` (Microphone + close-up camera) and ``Black``.
    """

    def __init__(self, program: str = "Main Camera"):
        from stream_pilot.obs import ObsRequestFailed

        self._fail = ObsRequestFailed
        self.inputs: dict[str, dict] = {
            "Microphone": {"kind": "pulse_input_capture", "settings": {"device_id": "mic"}},
            "Logitech Camera 1 (Main)": {"kind": "v4l2_input", "settings": {}},
            "Video Capture Device (V4L2)": {"kind": "v4l2_input", "settings": {}},
        }
        # scene -> list of {"id", "source", "enabled", "transform"} (index 0 = bottom)
        self.scenes: dict[str, list[dict]] = {
            "Black": [],
            "Jewlery Camera": [],
            "Main Camera": [],
        }
        self._next_id = 1
        for scene, sources in (
            ("Main Camera", ["Microphone", "Logitech Camera 1 (Main)"]),
            ("Jewlery Camera", ["Microphone", "Video Capture Device (V4L2)"]),
        ):
            for s in sources:
                self._add_item(scene, s, True)
        # source -> {filter name: {"kind", "settings", "enabled"}}
        self.filters: dict[str, dict[str, dict]] = {}
        self.program = program
        self.preview: str | None = None
        self.studio_mode = False
        self.swap_pending: str | None = None
        self.stats = {"activeFps": 30.0, "renderSkippedFrames": 0, "renderTotalFrames": 0,
                      "outputSkippedFrames": 0, "outputTotalFrames": 0, "cpuUsage": 5.0}
        self.stream_status = {"outputActive": False, "outputSkippedFrames": 0,
                              "outputTotalFrames": 0, "outputCongestion": 0.0}
        self.input_active: dict[str, bool] = {}

    def _add_item(self, scene: str, source: str, enabled: bool) -> int:
        item_id = self._next_id
        self._next_id += 1
        self.scenes[scene].append(
            {"id": item_id, "source": source, "enabled": enabled, "transform": {}}
        )
        return item_id

    def _scene(self, name: str) -> list[dict]:
        if name not in self.scenes:
            raise self._fail("scene", 600, f"No source was found by the name of `{name}`.")
        return self.scenes[name]

    def item(self, scene: str, source: str) -> dict | None:
        return next((i for i in self.scenes.get(scene, []) if i["source"] == source), None)

    def handle(self, rt: str, d: dict) -> dict | None:
        if rt == "GetSceneList":
            return {"scenes": [{"sceneName": n} for n in self.scenes],
                    "currentProgramSceneName": self.program,
                    "currentPreviewSceneName": self.preview}
        if rt == "GetInputList":
            return {"inputs": [{"inputName": n, "inputKind": i["kind"]}
                               for n, i in self.inputs.items()]}
        if rt == "CreateScene":
            if d["sceneName"] in self.scenes:
                raise self._fail(rt, 601, "exists")
            self.scenes[d["sceneName"]] = []
            return {}
        if rt == "GetSceneItemId":
            items = self._scene(d["sceneName"])
            for i in items:
                if i["source"] == d["sourceName"]:
                    return {"sceneItemId": i["id"]}
            raise self._fail(rt, 600, "No scene items were found")
        if rt == "GetSceneItemList":
            return {"sceneItems": [
                {"sceneItemId": i["id"], "sourceName": i["source"],
                 "sceneItemEnabled": i["enabled"],
                 "inputKind": self.inputs.get(i["source"], {}).get("kind")}
                for i in self._scene(d["sceneName"])]}
        if rt == "CreateInput":
            if d["inputName"] in self.inputs or d["inputName"] in self.scenes:
                raise self._fail(rt, 601, "exists")
            self._scene(d["sceneName"])
            self.inputs[d["inputName"]] = {"kind": d["inputKind"],
                                           "settings": dict(d.get("inputSettings") or {})}
            return {"sceneItemId": self._add_item(
                d["sceneName"], d["inputName"], d.get("sceneItemEnabled", True))}
        if rt == "CreateSceneItem":
            self._scene(d["sceneName"])
            if d["sourceName"] not in self.inputs and d["sourceName"] not in self.scenes:
                raise self._fail(rt, 600, "No source")
            return {"sceneItemId": self._add_item(
                d["sceneName"], d["sourceName"], d.get("sceneItemEnabled", True))}
        if rt in ("SetSceneItemEnabled", "SetSceneItemTransform", "RemoveSceneItem"):
            items = self._scene(d["sceneName"])
            item = next((i for i in items if i["id"] == d["sceneItemId"]), None)
            if item is None:
                raise self._fail(rt, 600, "No scene item")
            if rt == "SetSceneItemEnabled":
                item["enabled"] = d["sceneItemEnabled"]
            elif rt == "SetSceneItemTransform":
                item["transform"].update(d["sceneItemTransform"])
            else:
                items.remove(item)
            return {}
        if rt == "GetSceneItemTransform":
            item = next((i for i in self._scene(d["sceneName"]) if i["id"] == d["sceneItemId"]), None)
            if item is None:
                raise self._fail(rt, 600, "No scene item")
            base = {"positionX": 0.0, "positionY": 0.0, "rotation": 0.0, "scaleX": 1.0,
                    "scaleY": 1.0, "alignment": 5, "boundsType": "OBS_BOUNDS_NONE",
                    "sourceWidth": 1920.0, "width": 1920.0}
            return {"sceneItemTransform": {**base, **item["transform"]}}
        if rt == "GetInputSettings":
            if d["inputName"] not in self.inputs:
                raise self._fail(rt, 600, "No input")
            i = self.inputs[d["inputName"]]
            return {"inputKind": i["kind"], "inputSettings": dict(i["settings"])}
        if rt == "SetInputSettings":
            if d["inputName"] not in self.inputs:
                raise self._fail(rt, 600, "No input")
            self.inputs[d["inputName"]]["settings"].update(d["inputSettings"])
            return {}
        if rt == "GetSourceFilter":
            f = self.filters.get(d["sourceName"], {}).get(d["filterName"])
            if f is None:
                raise self._fail(rt, 600, "No filter")
            return {"filterKind": f["kind"], "filterEnabled": f["enabled"],
                    "filterSettings": dict(f["settings"])}
        if rt == "CreateSourceFilter":
            if d["sourceName"] not in self.inputs and d["sourceName"] not in self.scenes:
                raise self._fail(rt, 600, "No source")
            filters = self.filters.setdefault(d["sourceName"], {})
            if d["filterName"] in filters:
                raise self._fail(rt, 601, "exists")
            filters[d["filterName"]] = {"kind": d["filterKind"], "enabled": True,
                                        "settings": dict(d.get("filterSettings") or {})}
            return {}
        if rt == "SetSourceFilterEnabled":
            f = self.filters.get(d["sourceName"], {}).get(d["filterName"])
            if f is None:
                raise self._fail(rt, 600, "No filter")
            f["enabled"] = d["filterEnabled"]
            return {}
        if rt == "GetStudioModeEnabled":
            return {"studioModeEnabled": self.studio_mode}
        if rt == "SetStudioModeEnabled":
            self.studio_mode = d["studioModeEnabled"]
            self.preview = self.program if self.studio_mode else None
            self.swap_pending = None
            return {}
        if rt == "GetCurrentProgramScene":
            return {"currentProgramSceneName": self.program, "sceneName": self.program}
        if rt == "SetCurrentProgramScene":
            self._scene(d["sceneName"])
            old, self.program = self.program, d["sceneName"]
            if self.studio_mode:
                self.swap_pending = old  # OBS swaps when the transition ends (same scene too)
            return {}
        if rt == "GetCurrentPreviewScene":
            if not self.studio_mode:
                raise self._fail(rt, 506, "Studio mode is not active")
            return {"currentPreviewSceneName": self.preview, "sceneName": self.preview}
        if rt == "SetCurrentPreviewScene":
            if not self.studio_mode:
                raise self._fail(rt, 506, "Studio mode is not active")
            self._scene(d["sceneName"])
            self.preview = d["sceneName"]
            if self.swap_pending is not None:  # the transition ends after this request
                self.preview, self.swap_pending = self.swap_pending, None
            return {}
        if rt == "GetCurrentSceneTransition":
            return {"transitionName": "Fade", "transitionDuration": 300}
        if rt == "GetStats":
            return dict(self.stats)
        if rt == "GetStreamStatus":
            return dict(self.stream_status)
        if rt == "GetSourceActive":
            name = d["sourceName"]
            if name not in self.inputs and name not in self.scenes:
                raise self._fail(rt, 600, "No source")
            active = self.input_active.get(name, True)
            return {"videoActive": active, "videoShowing": active}
        raise self._fail(rt, 204, f"FakeObs: unknown request {rt}")


class FakeFacebook(Gated):
    def __init__(
        self,
        *,
        connect_raises: Any = None,
        reachable: Any = True,
        logged_in: Any = True,
        setup_raises: Any = None,
        target: StreamTarget | None = None,
        preview_raises: Any = None,
        preview_lost: Any = False,
        preview_lost_raises: Any = None,
        live_id: str = "123456789",
        go_live_raises: Any = None,
        open_comments_raises: Any = None,
        park_raises: Any = None,
        save_failure_raises: Any = None,
        close_raises: Any = None,
        refresh_ssn_raises: Any = None,
        calls: list | None = None,
    ):
        self._init_gated()
        self.calls: list[tuple] = calls if calls is not None else []
        self.target = target or StreamTarget("rtmps://fb.example/rtmp/", "KEY")
        self.live_id = live_id
        self._connect = Script(connect_raises)
        self._reachable = Script(reachable)
        self._logged_in = Script(logged_in)
        self._setup = Script(setup_raises)
        self._preview = Script(preview_raises)
        self._preview_lost = Script(preview_lost)
        self._preview_lost_raises = Script(preview_lost_raises)
        self._go_live = Script(go_live_raises)
        self._open_comments = Script(open_comments_raises)
        self._park = Script(park_raises)
        self._save_failure = Script(save_failure_raises)
        self._close = Script(close_raises)
        self._refresh_ssn = Script(refresh_ssn_raises)
        self.set_title_raises: Exception | None = None

    async def connect(self) -> None:
        self.calls.append(("fb.connect",))
        self._connect.raise_next()

    async def reachable(self) -> bool:
        self.calls.append(("fb.reachable",))
        await self._gated("reachable")
        return self._reachable.next()

    async def refresh_ssn(self) -> None:
        self.calls.append(("fb.refresh_ssn",))
        self._refresh_ssn.raise_next()

    async def ensure_ssn_tabs(self) -> list[str]:
        self.calls.append(("fb.ensure_ssn_tabs",))
        return []

    async def logged_in(self) -> bool:
        self.calls.append(("fb.logged_in",))
        val = self._logged_in.next()
        if isinstance(val, BaseException):
            raise val
        return val

    async def setup_live(self) -> StreamTarget:
        self.calls.append(("fb.setup_live",))
        self._setup.raise_next()
        return self.target

    async def wait_preview_ready(self, timeout_s: float) -> None:
        self.calls.append(("fb.wait_preview_ready", timeout_s))
        await self._gated("wait_preview_ready")
        self._preview.raise_next()

    async def preview_lost(self) -> bool:
        self.calls.append(("fb.preview_lost",))
        await self._gated("preview_lost")
        self._preview_lost_raises.raise_next()
        return self._preview_lost.next()

    async def go_live(self, timeout_s: float) -> str:
        self.calls.append(("fb.go_live", timeout_s))
        await self._gated("go_live")
        self._go_live.raise_next()
        return self.live_id

    async def set_title(self, title: str) -> None:
        self.calls.append(("fb.set_title", title))
        await self._gated("set_title")
        if self.set_title_raises is not None:
            raise self.set_title_raises

    async def open_comments(self, live_id: str) -> None:
        self.calls.append(("fb.open_comments", live_id))
        await self._gated("open_comments")
        self._open_comments.raise_next()

    async def park_dashboard(self) -> None:
        self.calls.append(("fb.park_dashboard",))
        self._park.raise_next()

    async def save_failure(self, step: str, directory: Path) -> None:
        self.calls.append(("fb.save_failure", step, directory))
        self._save_failure.raise_next()

    async def close_browser(self) -> None:
        self.calls.append(("fb.close_browser",))
        await self._gated("close_browser")
        self._close.raise_next()


class FakeCapture(Gated):
    def __init__(
        self,
        *,
        start_raises: Any = None,
        stop_raises: Any = None,
        calls: list | None = None,
    ):
        self._init_gated()
        self.calls: list[tuple] = calls if calls is not None else []
        self._start = Script(start_raises)
        self._stop = Script(stop_raises)

    async def start(self, live_id: str, stream_dir: Path) -> None:
        self.calls.append(("capture.start", live_id, stream_dir))
        self._start.raise_next()

    async def stop(self) -> None:
        self.calls.append(("capture.stop",))
        await self._gated("stop")
        self._stop.raise_next()


class FakeDeck:
    def __init__(self, *, calls: list | None = None):
        self.calls: list[tuple] = calls if calls is not None else []
        self.faces: dict[int, KeyFace] = {}
        self.sink = None

    def set_event_sink(self, sink) -> None:
        self.sink = sink

    def show(self, key: int, face: KeyFace) -> None:
        self.calls.append(("deck.show", key, face.label))
        self.faces[key] = face

    async def start(self) -> None:
        self.calls.append(("deck.start",))


class FakeSystem(Gated):
    def __init__(
        self,
        *,
        stop_unit_raises: Any = None,
        poweroff_raises: Any = None,
        calls: list | None = None,
    ):
        self._init_gated()
        self.calls: list[tuple] = calls if calls is not None else []
        self._stop_unit = Script(stop_unit_raises)
        self._poweroff = Script(poweroff_raises)
        self.active_units: set[str] = set()

    async def poweroff(self) -> None:
        self.calls.append(("system.poweroff",))
        self._poweroff.raise_next()

    async def stop_user_unit(self, unit: str) -> None:
        self.calls.append(("system.stop_user_unit", unit))
        await self._gated("stop_user_unit")
        self._stop_unit.raise_next()
        if unit.endswith("@*.service"):
            prefix = unit[: -len("*.service")]
            self.active_units = {u for u in self.active_units if not u.startswith(prefix)}
        else:
            self.active_units.discard(unit)

    async def set_window_layout(self, enabled: bool) -> None:
        self.calls.append(("system.set_window_layout", enabled))

    async def stop_user_unit_no_block(self, unit: str) -> None:
        self.calls.append(("system.stop_user_unit_no_block", unit))
        self.active_units.discard(unit)

    async def start_user_unit(self, unit: str) -> bool:
        self.calls.append(("system.start_user_unit", unit))
        await self._gated("start_user_unit")
        self.active_units.add(unit)
        return True

    async def user_unit_active(self, unit: str) -> bool:
        self.calls.append(("system.user_unit_active", unit))
        return unit in self.active_units


class FakeCamera(Gated):
    def __init__(
        self,
        *,
        raises: Any = None,
        calls: list | None = None,
        autofocus: dict[str, bool] | None = None,
    ):
        self._init_gated()
        self.calls: list[tuple] = calls if calls is not None else []
        self._raises = Script(raises)
        # None: the camera port is not read at all (get_autofocus not recorded)
        self._autofocus = autofocus

    async def get_autofocus(self, device: str) -> bool | None:
        if self._autofocus is None:
            return None
        self.calls.append(("camera.get_autofocus", device))
        await self._gated("get_autofocus")
        return self._autofocus.get(device)

    async def set_autofocus(self, device: str, on: bool) -> None:
        self.calls.append(("camera.set_autofocus", device, on))
        await self._gated("set_autofocus")
        self._raises.raise_next()

    async def set_macro(self, device: str) -> None:
        self.calls.append(("camera.set_macro", device))
        await self._gated("set_macro")
        self._raises.raise_next()


__all__ = [
    "ObsModel",
    "FakeCamera",
    "FakeCapture",
    "FakeDeck",
    "FakeFacebook",
    "FakeObs",
    "FakeSystem",
    "Script",
    "StepFailed",
]
