"""Tests for the ``run`` wiring (app.py).

Host safety: every adapter class is monkeypatched, so nothing here opens a
device, connects to OBS or runs systemctl.
"""

import asyncio
import contextlib
import logging
import os
import signal

import pytest

from stream_pilot import app
from stream_pilot.browser import CdpBrowser
from stream_pilot.config import load_config
from stream_pilot.controller import Controller
from stream_pilot.events import DeckPress, RetryRequested, Tick
from stream_pilot.fb.adapter import CaptureAdapter, FacebookAdapter
from stream_pilot.persist import StateStore, StatusWriter
from stream_pilot.ports import StepFailed

from fakes import (
    FakeCamera,
    FakeCapture,
    FakeDeck,
    FakeFacebook,
    FakeObs,
    FakeSystem,
)

SITE = """
[paths]
state_dir = "{root}/state"
streams_dir = "{root}/streams"
failures_dir = "{root}/failures"
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    site = tmp_path / "site.toml"
    site.write_text(SITE.format(root=tmp_path.as_posix()))
    return load_config([site])


class FakePower:
    def __init__(self):
        self.sink = None

    def set_event_sink(self, sink) -> None:
        self.sink = sink

    async def start(self) -> None:
        pass


class FakeMemoryGuard:
    instances: list = []

    def __init__(self, cfg, deck, on_sample):
        self.cfg = cfg
        self.deck = deck
        self.on_sample = on_sample
        FakeMemoryGuard.instances.append(self)

    async def run(self) -> None:
        pass


class FakeSsnWatch:
    def __init__(self):
        self.sink = None

    def set_event_sink(self, sink):
        self.sink = sink

    async def start(self) -> None:
        pass


@pytest.fixture
def adapters(monkeypatch):
    """Patch every real adapter class on ``stream_pilot.app`` with a fake."""
    made: dict = {}

    def register(name, factory):
        def build(*args, **kwargs):
            obj = factory(*args, **kwargs)
            made[name] = (obj, args, kwargs)
            return obj

        monkeypatch.setattr(app, name, build)

    FakeMemoryGuard.instances = []
    register("SimpleObsClient", lambda cfg: FakeObs())
    register("StreamDeckAdapter", lambda cfg: FakeDeck())
    register("PowerKeyListener", lambda: FakePower())
    register("SsnStallWatcher", lambda: FakeSsnWatch())
    register("SystemCtl", lambda: FakeSystem())
    register("V4l2Camera", lambda: FakeCamera())
    register("MemoryGuard", FakeMemoryGuard)
    return made


async def close_all(coros) -> None:
    for c in coros:
        c.close()


# ----------------------------------------------------------------- build_app


async def test_build_app_wires_ports_and_sinks(cfg, adapters):
    fb, cap = FakeFacebook(), FakeCapture()
    controller, coros = app.build_app(cfg, dry_run=True, facebook=fb, capture=cap)
    try:
        assert isinstance(controller, Controller)
        assert controller.cfg is cfg
        assert controller.dry_run is True
        assert controller.fb is fb
        assert controller.capture is cap
        assert controller.obs is adapters["SimpleObsClient"][0]
        assert controller.deck is adapters["StreamDeckAdapter"][0]
        assert controller.system is adapters["SystemCtl"][0]
        assert controller.camera is adapters["V4l2Camera"][0]
        # adapters were constructed from the right config sections
        assert adapters["SimpleObsClient"][1] == (cfg.obs,)
        assert adapters["StreamDeckAdapter"][1] == (cfg.deck,)
        # every event source feeds the controller
        assert controller.obs.sink == controller.sink
        assert controller.deck.sink == controller.sink
        assert adapters["PowerKeyListener"][0].sink == controller.sink
        assert adapters["SsnStallWatcher"][0].sink == controller.sink
        # memory guard: cfg.memory, the real deck, samples go to the controller
        (guard,) = FakeMemoryGuard.instances
        assert guard.cfg is cfg.memory
        assert guard.deck is controller.deck
        assert guard.on_sample == controller.note_memory
        # persistence under state_dir
        assert isinstance(controller.store, StateStore)
        assert controller.store.path == cfg.paths.state_dir / "state.json"
        assert isinstance(controller.status, StatusWriter)
        assert controller.status.path == cfg.paths.state_dir / "status.json"
        assert len(coros) == 5
    finally:
        await close_all(coros)


async def test_build_app_default_dry_run_flag(cfg, adapters):
    controller, coros = app.build_app(cfg, dry_run=False)
    try:
        assert controller.dry_run is False
    finally:
        await close_all(coros)


async def test_build_app_defaults_to_the_real_browser_adapters(cfg, adapters):
    controller, coros = app.build_app(cfg, dry_run=False)
    try:
        assert isinstance(controller.fb, FacebookAdapter)
        assert isinstance(controller.capture, CaptureAdapter)
        # one shared browser connection; nothing connects at construction time
        assert controller.fb._browser is controller.capture._browser
        assert isinstance(controller.fb._browser, CdpBrowser)
        assert controller.fb._browser.cfg is cfg.cdp
        assert not controller.fb._browser.is_connected
        assert controller.fb._record_dir is None
    finally:
        await close_all(coros)


async def test_build_app_passes_record_dir_to_the_facebook_adapter(cfg, adapters, tmp_path):
    controller, coros = app.build_app(cfg, dry_run=False, record_dir=tmp_path / "rec")
    try:
        assert controller.fb._record_dir == tmp_path / "rec"
    finally:
        await close_all(coros)


async def test_build_app_uses_injected_ports_instead_of_real_adapters(cfg, adapters, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("real adapter constructed")

    monkeypatch.setattr(app, "CdpBrowser", boom)
    fb, cap = FakeFacebook(), FakeCapture()
    controller, coros = app.build_app(cfg, dry_run=True, facebook=fb, capture=cap)
    try:
        assert controller.fb is fb and controller.capture is cap
    finally:
        await close_all(coros)


async def test_build_app_mixes_one_injected_and_one_real_port(cfg, adapters):
    cap = FakeCapture()
    controller, coros = app.build_app(cfg, dry_run=True, capture=cap)
    try:
        assert controller.capture is cap
        assert isinstance(controller.fb, FacebookAdapter)
    finally:
        await close_all(coros)


async def test_background_coroutines_cover_deck_power_ssn_watch_memory_and_ticker(
    cfg, adapters, monkeypatch
):
    ran: list[str] = []
    deck_obj = FakeDeck()
    power_obj = FakePower()

    async def deck_start():
        ran.append("deck")

    async def power_start():
        ran.append("power")

    class Guard(FakeMemoryGuard):
        async def run(self):
            ran.append("memory")

    ssn_obj = FakeSsnWatch()

    async def ssn_start():
        ran.append("ssn-watch")

    deck_obj.start = deck_start
    power_obj.start = power_start
    ssn_obj.start = ssn_start
    monkeypatch.setattr(app, "StreamDeckAdapter", lambda c: deck_obj)
    monkeypatch.setattr(app, "PowerKeyListener", lambda: power_obj)
    monkeypatch.setattr(app, "SsnStallWatcher", lambda: ssn_obj)
    monkeypatch.setattr(app, "MemoryGuard", Guard)

    controller, coros = app.build_app(cfg, dry_run=False)
    tasks = [asyncio.ensure_future(c) for c in coros]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        assert sorted(ran) == ["deck", "memory", "power", "ssn-watch"]
        # the fifth one is the ticker: still running, never finishes by itself
        assert sum(1 for t in tasks if not t.done()) == 1
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_ticker_sends_tick_every_15_seconds():
    sent = []
    sleeps = []

    class C:
        def sink(self, event):
            sent.append(event)

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await app._ticker(C(), sleep=fake_sleep)
    assert sleeps == [15, 15, 15]
    assert sent == [Tick(), Tick()]


# --------------------------------------------------------------- _supervised


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


async def test_supervised_restarts_with_exponential_backoff_capped_at_60():
    clock = Clock()
    sleeps = []
    runs = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 9:
            raise asyncio.CancelledError

    async def crash():
        runs.append(1)
        raise OSError("no hid")

    with pytest.raises(asyncio.CancelledError):
        await app._supervised("deck", crash, sleep=fake_sleep, clock=clock)
    assert sleeps == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert len(runs) == 9


async def test_supervised_resets_backoff_after_running_over_60_seconds():
    clock = Clock()
    sleeps = []
    durations = iter([0, 0, 0, 61, 0, 0])  # how long each run "lasted"

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 6:
            raise asyncio.CancelledError

    async def crash():
        clock.now += next(durations)
        raise OSError("x")

    with pytest.raises(asyncio.CancelledError):
        await app._supervised("deck", crash, sleep=fake_sleep, clock=clock)
    # 4th run lasted > 60 s: its backoff restarts from 1 s
    assert sleeps == [1, 2, 4, 1, 2, 4]


async def test_supervised_exactly_60_seconds_does_not_reset():
    clock = Clock()
    sleeps = []
    durations = iter([0, 60, 0])

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    async def crash():
        clock.now += next(durations)
        raise OSError("x")

    with pytest.raises(asyncio.CancelledError):
        await app._supervised("deck", crash, sleep=fake_sleep, clock=clock)
    assert sleeps == [1, 2, 4]


async def test_supervised_propagates_cancellation_without_restart():
    runs = []

    async def blocks():
        runs.append(1)
        await asyncio.Event().wait()

    task = asyncio.ensure_future(app._supervised("x", blocks))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runs == [1]


async def test_supervised_cancel_during_backoff_propagates():
    async def crash():
        raise OSError("x")

    task = asyncio.ensure_future(app._supervised("x", crash))  # real 1 s backoff
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_supervised_normal_return_is_not_restarted():
    runs = []

    async def returns():
        runs.append(1)

    await app._supervised("deck", returns)
    assert runs == [1]


async def test_supervised_logs_traceback_at_error_level(caplog):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        raise asyncio.CancelledError

    async def crash():
        raise ImportError("no evdev")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(asyncio.CancelledError):
            await app._supervised("power", crash, sleep=fake_sleep)
    rec = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert rec and rec[0].exc_info
    assert "power" in rec[0].getMessage()


async def test_build_app_background_awaitables_restart_crashing_adapters(
    cfg, adapters, monkeypatch
):
    monkeypatch.setattr(app, "BACKOFF_INITIAL_S", 0.001)
    monkeypatch.setattr(app, "BACKOFF_MAX_S", 0.004)
    power = FakePower()
    monkeypatch.setattr(app, "PowerKeyListener", lambda: power)
    attempts = []
    running = asyncio.Event()

    async def start():
        attempts.append(1)
        if len(attempts) <= 2:
            raise ImportError("No module named 'evdev'")
        running.set()
        await asyncio.Event().wait()

    power.start = start
    controller, coros = app.build_app(cfg, dry_run=False)
    assert len(coros) == 5
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        await asyncio.wait_for(running.wait(), 2)
        assert len(attempts) == 3
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ----------------------------------------------------------------- NotWired


@pytest.mark.parametrize(
    "call",
    [
        lambda f: f.reachable(),
        lambda f: f.logged_in(),
        lambda f: f.setup_live(),
        lambda f: f.wait_preview_ready(1.0),
        lambda f: f.preview_lost(),
        lambda f: f.go_live(1.0),
        lambda f: f.open_comments("1"),
        lambda f: f.park_dashboard(),
        lambda f: f.save_failure("x", None),
    ],
)
async def test_not_wired_facebook_raises(call):
    with pytest.raises(StepFailed) as exc:
        await call(app.NotWiredFacebook())
    assert exc.value.step == "facebook"
    assert exc.value.detail == "not wired"


async def test_not_wired_facebook_connect_and_close_are_noops():
    fb = app.NotWiredFacebook()
    assert await fb.connect() is None
    assert await fb.close_browser() is None


async def test_not_wired_capture():
    cap = app.NotWiredCapture()
    with pytest.raises(StepFailed) as exc:
        await cap.start("1", None)
    assert (exc.value.step, exc.value.detail) == ("facebook", "not wired")
    assert await cap.stop() is None


# ------------------------------------------------------------------- run_app


@pytest.fixture
def restore_signals():
    saved = {
        s: signal.getsignal(s)
        for s in (signal.SIGUSR1, signal.SIGUSR2, signal.SIGRTMIN, signal.SIGTERM, signal.SIGINT)
    }
    yield
    for s, h in saved.items():
        signal.signal(s, h)


class FakeController:
    def __init__(self, *, run_exc=None, run_delay=0):
        self.run_delay = run_delay
        self.events: list = []
        self.started = asyncio.Event()
        self.cancelled = False
        self.run_exc = run_exc
        self.deck = None

    def sink(self, event) -> None:
        self.events.append(event)

    async def run(self) -> None:
        self.started.set()
        if self.run_exc is not None:
            await asyncio.sleep(self.run_delay)
            raise self.run_exc
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def patch_build(monkeypatch, controller, extra=()):
    async def forever():
        await asyncio.Event().wait()

    def fake_build(cfg, *, dry_run, facebook=None, capture=None, record_dir=None):
        fake_build.dry_run = dry_run
        fake_build.record_dir = record_dir
        return controller, [forever(), *extra]

    monkeypatch.setattr(app, "build_app", fake_build)
    return fake_build


@contextlib.asynccontextmanager
async def running_app(cfg, controller, dry_run=False, record_dir=None):
    """Run ``run_app`` as a task; always shuts it down (SIGTERM) on exit.

    Guarantees a failing assertion cannot leak signal handlers or a live task.
    """
    task = asyncio.ensure_future(app.run_app(cfg, dry_run, record_dir=record_dir))
    try:
        await asyncio.wait_for(controller.started.wait(), 2)
        yield task
    finally:
        if not task.done():
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            finally:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


async def test_run_app_sigusr1_enqueues_retry_and_sigterm_exits_zero(
    cfg, monkeypatch, restore_signals
):
    controller = FakeController()
    build = patch_build(monkeypatch, controller)
    async with running_app(cfg, controller, dry_run=True) as task:
        os.kill(os.getpid(), signal.SIGUSR1)
        for _ in range(200):
            if controller.events:
                break
            await asyncio.sleep(0.01)
        assert controller.events == [RetryRequested()]

        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(task, 5) == 0
    assert controller.cancelled
    assert build.dry_run is True


async def test_run_app_sigusr2_enqueues_a_status_key_press(cfg, monkeypatch, restore_signals):
    controller = FakeController()
    patch_build(monkeypatch, controller)
    async with running_app(cfg, controller):
        os.kill(os.getpid(), signal.SIGUSR2)
        for _ in range(200):
            if controller.events:
                break
            await asyncio.sleep(0.01)
        assert controller.events == [DeckPress(0)]


async def test_run_app_sigrtmin_enqueues_a_status_key_long_press(cfg, monkeypatch, restore_signals):
    controller = FakeController()
    patch_build(monkeypatch, controller)
    async with running_app(cfg, controller):
        os.kill(os.getpid(), signal.SIGRTMIN)
        for _ in range(200):
            if controller.events:
                break
            await asyncio.sleep(0.01)
        assert controller.events == [DeckPress(0, long=True)]


async def test_run_app_passes_record_dir_to_build_app(cfg, monkeypatch, restore_signals, tmp_path):
    controller = FakeController()
    build = patch_build(monkeypatch, controller)
    async with running_app(cfg, controller, record_dir=tmp_path / "rec") as task:
        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(task, 5) == 0
    assert build.record_dir == tmp_path / "rec"


async def test_run_app_record_dir_defaults_to_none(cfg, monkeypatch, restore_signals):
    controller = FakeController()
    build = patch_build(monkeypatch, controller)
    async with running_app(cfg, controller) as task:
        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(task, 5) == 0
    assert build.record_dir is None


async def test_run_app_sigint_also_exits_zero(cfg, monkeypatch, restore_signals):
    controller = FakeController()
    patch_build(monkeypatch, controller)
    async with running_app(cfg, controller) as task:
        os.kill(os.getpid(), signal.SIGINT)
        assert await asyncio.wait_for(task, 5) == 0


async def test_run_app_controller_crash_logs_cancels_rest_and_returns_nonzero(
    cfg, monkeypatch, restore_signals, caplog
):
    controller = FakeController(run_exc=RuntimeError("controller exploded"), run_delay=0.05)
    other = {"cancelled": False}

    async def other_background():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            other["cancelled"] = True
            raise

    patch_build(monkeypatch, controller, extra=[other_background()])
    with caplog.at_level(logging.ERROR):
        rc = await asyncio.wait_for(app.run_app(cfg, False), 5)
    assert rc != 0
    assert other["cancelled"]
    assert "controller exploded" in caplog.text
    assert any(r.exc_info for r in caplog.records)


async def test_run_app_crashing_background_task_does_not_take_down_controller(
    cfg, monkeypatch, restore_signals, caplog
):
    """A missing Stream Deck / evdev must never end the stream controller."""
    monkeypatch.setattr(app, "BACKOFF_INITIAL_S", 0.001)
    monkeypatch.setattr(app, "BACKOFF_MAX_S", 0.004)
    attempts = []
    blocked = asyncio.Event()

    async def power_start():
        attempts.append(1)
        if len(attempts) <= 2:
            raise ImportError("No module named 'evdev'")
        blocked.set()
        await asyncio.Event().wait()

    controller = FakeController()
    patch_build(
        monkeypatch, controller, extra=[app._supervised("power", power_start)]
    )
    with caplog.at_level(logging.ERROR):
        async with running_app(cfg, controller) as task:
            await asyncio.wait_for(blocked.wait(), 2)
            assert len(attempts) == 3
            assert not task.done()
            assert not controller.cancelled
            os.kill(os.getpid(), signal.SIGTERM)
            assert await asyncio.wait_for(task, 5) == 0  # clean exit, not a crash
    assert caplog.text.count("evdev") >= 2
    assert any(r.exc_info for r in caplog.records)


async def test_run_app_controller_crash_returns_nonzero(
    cfg, monkeypatch, restore_signals
):
    controller = FakeController(run_exc=RuntimeError("controller died"))
    patch_build(monkeypatch, controller)
    assert await asyncio.wait_for(app.run_app(cfg, False), 5) != 0


async def test_run_app_background_task_returning_normally_is_fine(
    cfg, monkeypatch, restore_signals
):
    """deck.start() spawns its own task and returns: that is not a failure."""

    async def returns():
        return None

    controller = FakeController()
    patch_build(monkeypatch, controller, extra=[returns()])
    async with running_app(cfg, controller) as task:
        await asyncio.sleep(0.05)
        assert not task.done()
        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(task, 5) == 0


async def test_run_app_stops_deck_adapter_on_shutdown(cfg, monkeypatch, restore_signals):
    stopped = []

    class Deck:
        async def stop(self):
            stopped.append(True)

    controller = FakeController()
    controller.deck = Deck()
    patch_build(monkeypatch, controller)
    async with running_app(cfg, controller) as task:
        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(task, 5) == 0
    assert stopped == [True]


async def test_run_app_configures_logging_to_stderr(cfg, monkeypatch, restore_signals):
    calls = []
    monkeypatch.setattr(app.logging, "basicConfig", lambda **kw: calls.append(kw))
    controller = FakeController(run_exc=RuntimeError("stop now"))
    patch_build(monkeypatch, controller)
    await asyncio.wait_for(app.run_app(cfg, False), 5)
    assert len(calls) == 1
    assert calls[0]["level"] == logging.INFO
    assert calls[0]["stream"] is app.sys.stderr
    assert calls[0]["format"] == "%(levelname)s %(name)s: %(message)s"


async def test_run_app_removes_signal_handlers_when_done(
    cfg, monkeypatch, restore_signals
):
    controller = FakeController(run_exc=RuntimeError("stop now"))
    patch_build(monkeypatch, controller)
    await asyncio.wait_for(app.run_app(cfg, False), 5)
    loop = asyncio.get_running_loop()
    assert not loop.remove_signal_handler(signal.SIGUSR1)  # none left installed
