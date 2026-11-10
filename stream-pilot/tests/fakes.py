"""In-memory fakes for the controller ports.

Every fake records its calls in ``self.calls`` (a list of tuples). Pass a shared
list as ``calls=`` to several fakes to assert cross-object call order.

Configurable results use "script" values: a plain value is returned/raised on
every call; a list is consumed one entry per call and the last entry repeats.
For ``*_raises`` options ``None`` means "do not raise"; an exception instance is
raised.
"""

from __future__ import annotations

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


class FakeObs:
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

    def set_event_sink(self, sink) -> None:
        self.calls.append(("obs.set_event_sink",))
        self.sink = sink

    async def connect(self) -> None:
        self.calls.append(("obs.connect",))
        self._connect.raise_next()

    async def stream_active(self) -> bool:
        self.calls.append(("obs.stream_active",))
        self._active.raise_next()
        return self.active

    async def set_stream_target(self, target: StreamTarget) -> None:
        self.calls.append(("obs.set_stream_target", target))

    async def start_stream(self) -> None:
        self.calls.append(("obs.start_stream",))
        self._start.raise_next()
        self.active = True

    async def stop_stream(self) -> None:
        self.calls.append(("obs.stop_stream",))
        self._stop.raise_next()
        if not self.stop_keeps_active:
            self.active = False

    async def set_scene(self, name: str) -> None:
        self.calls.append(("obs.set_scene", name))
        self._set_scene.raise_next()
        self.scene = name

    async def current_scene(self) -> str:
        self.calls.append(("obs.current_scene",))
        return self.scene


class FakeFacebook:
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
        calls: list | None = None,
    ):
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

    async def connect(self) -> None:
        self.calls.append(("fb.connect",))
        self._connect.raise_next()

    async def reachable(self) -> bool:
        self.calls.append(("fb.reachable",))
        return self._reachable.next()

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
        self._preview.raise_next()

    async def preview_lost(self) -> bool:
        self.calls.append(("fb.preview_lost",))
        self._preview_lost_raises.raise_next()
        return self._preview_lost.next()

    async def go_live(self, timeout_s: float) -> str:
        self.calls.append(("fb.go_live", timeout_s))
        self._go_live.raise_next()
        return self.live_id

    async def open_comments(self, live_id: str) -> None:
        self.calls.append(("fb.open_comments", live_id))
        self._open_comments.raise_next()

    async def park_dashboard(self) -> None:
        self.calls.append(("fb.park_dashboard",))
        self._park.raise_next()

    async def save_failure(self, step: str, directory: Path) -> None:
        self.calls.append(("fb.save_failure", step, directory))
        self._save_failure.raise_next()

    async def close_browser(self) -> None:
        self.calls.append(("fb.close_browser",))
        self._close.raise_next()


class FakeCapture:
    def __init__(
        self,
        *,
        start_raises: Any = None,
        stop_raises: Any = None,
        calls: list | None = None,
    ):
        self.calls: list[tuple] = calls if calls is not None else []
        self._start = Script(start_raises)
        self._stop = Script(stop_raises)

    async def start(self, live_id: str, stream_dir: Path) -> None:
        self.calls.append(("capture.start", live_id, stream_dir))
        self._start.raise_next()

    async def stop(self) -> None:
        self.calls.append(("capture.stop",))
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


class FakeSystem:
    def __init__(
        self,
        *,
        stop_unit_raises: Any = None,
        poweroff_raises: Any = None,
        calls: list | None = None,
    ):
        self.calls: list[tuple] = calls if calls is not None else []
        self._stop_unit = Script(stop_unit_raises)
        self._poweroff = Script(poweroff_raises)

    async def poweroff(self) -> None:
        self.calls.append(("system.poweroff",))
        self._poweroff.raise_next()

    async def stop_user_unit(self, unit: str) -> None:
        self.calls.append(("system.stop_user_unit", unit))
        self._stop_unit.raise_next()


class FakeCamera:
    def __init__(self, *, raises: Any = None, calls: list | None = None):
        self.calls: list[tuple] = calls if calls is not None else []
        self._raises = Script(raises)

    async def set_autofocus(self, device: str, on: bool) -> None:
        self.calls.append(("camera.set_autofocus", device, on))
        self._raises.raise_next()


__all__ = [
    "FakeCamera",
    "FakeCapture",
    "FakeDeck",
    "FakeFacebook",
    "FakeObs",
    "FakeSystem",
    "Script",
    "StepFailed",
]
