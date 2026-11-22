"""Wiring for ``stream-pilot run``: real adapters + controller + background tasks.

``build_app`` is synchronous and side-effect free apart from constructing the
adapters (none of which touch a device until their ``start()``/``connect()``
runs). ``run_app`` owns the process lifecycle: logging, signal handling and
supervising the long-running tasks.

Shutdown policy: SIGTERM/SIGINT cancel everything and exit 0, but never stop
the OBS stream (Facebook would end the live); a task dying with an unexpected
exception cancels the rest and exits non-zero so systemd's ``Restart=always``
restarts the controller (which resumes a persisted live).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

from .browser import CdpBrowser
from .config import Config
from .controller import Controller
from .deck import StreamDeckAdapter
from .events import RetryRequested, Tick
from .fb.adapter import CaptureAdapter, FacebookAdapter
from .memory import MemoryGuard
from .obs import SimpleObsClient
from .persist import StateStore, StatusWriter
from .ports import CommentCapturePort, FacebookPort, StepFailed
from .power import PowerKeyListener
from .system import SystemCtl, V4l2Camera

log = logging.getLogger(__name__)

TICK_INTERVAL_S = 15
_DECK_STOP_TIMEOUT_S = 5
# Keep-alive backoff for background tasks (module constants so tests can shrink them).
BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 60.0
BACKOFF_RESET_AFTER_S = 60.0


def _not_wired() -> StepFailed:
    return StepFailed("facebook", "not wired")


class NotWiredFacebook:
    """Placeholder port (``build_app`` now wires the real browser adapters).

    Every operation raises ``StepFailed("facebook", "not wired")`` except the
    ones the controller needs to stay usable: ``connect`` (so STARTING can be
    left) and ``close_browser`` (so the power button still shuts down).
    """

    async def connect(self) -> None:
        return None

    async def reachable(self) -> bool:
        raise _not_wired()

    async def refresh_ssn(self) -> None:
        return None

    async def logged_in(self) -> bool:
        raise _not_wired()

    async def setup_live(self):
        raise _not_wired()

    async def wait_preview_ready(self, timeout_s: float) -> None:
        raise _not_wired()

    async def preview_lost(self) -> bool:
        raise _not_wired()

    async def go_live(self, timeout_s: float) -> str:
        raise _not_wired()

    async def open_comments(self, live_id: str) -> None:
        raise _not_wired()

    async def park_dashboard(self) -> None:
        raise _not_wired()

    async def save_failure(self, step: str, directory: Path) -> None:
        raise _not_wired()

    async def close_browser(self) -> None:
        return None


class NotWiredCapture:
    """Placeholder comment capture; ``stop`` is a no-op so the end flow works."""

    async def start(self, live_id: str, stream_dir: Path) -> None:
        raise _not_wired()

    async def stop(self) -> None:
        return None


async def _ticker(
    controller: Controller,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Heartbeat: a Tick every 15 s keeps status.json fresh (health check)."""
    while True:
        await sleep(TICK_INTERVAL_S)
        controller.sink(Tick())


async def _supervised(
    name: str,
    factory: Callable[[], Awaitable[None]],
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Keep a background task alive: restart it after an unexpected exception.

    A missing Stream Deck or evdev must never take the stream controller down.
    Backoff doubles from BACKOFF_INITIAL_S up to BACKOFF_MAX_S and resets once an
    attempt has run for longer than BACKOFF_RESET_AFTER_S. A normal return ends
    the supervisor (``deck.start()`` spawns its own task and returns).
    Cancellation propagates, including during the backoff sleep.
    """
    backoff = BACKOFF_INITIAL_S
    while True:
        started = clock()
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background task %s died; restarting in %.1fs", name, backoff)
        else:
            return
        if clock() - started > BACKOFF_RESET_AFTER_S:
            backoff = BACKOFF_INITIAL_S
        await (sleep or asyncio.sleep)(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX_S)


def build_app(
    cfg: Config,
    *,
    dry_run: bool,
    facebook: FacebookPort | None = None,
    capture: CommentCapturePort | None = None,
    record_dir: Path | None = None,
) -> tuple[Controller, list[Awaitable]]:
    """Build the controller with real adapters; return it plus background coroutines.

    When ``facebook`` / ``capture`` are None they are the CDP browser adapters
    sharing one ``CdpBrowser`` (constructing it connects to nothing: the
    controller calls ``facebook.connect()``). ``record_dir`` makes the
    Facebook flows save screenshots/aria snapshots there (``run --record``).
    """
    browser: CdpBrowser | None = None
    if facebook is None or capture is None:
        browser = CdpBrowser(cfg.cdp)
    if facebook is None:
        facebook = FacebookAdapter(cfg, browser, record_dir=record_dir)
    if capture is None:
        capture = CaptureAdapter(cfg, browser)
    obs = SimpleObsClient(cfg.obs)
    deck = StreamDeckAdapter(cfg.deck)
    power = PowerKeyListener()
    system = SystemCtl()
    camera = V4l2Camera()
    state_dir = cfg.paths.state_dir

    controller = Controller(
        cfg,
        obs=obs,
        fb=facebook,
        capture=capture,
        deck=deck,
        system=system,
        camera=camera,
        store=StateStore(state_dir / "state.json"),
        status=StatusWriter(state_dir / "status.json"),
        dry_run=dry_run,
    )
    for source in (obs, deck, power):
        source.set_event_sink(controller.sink)
    guard = MemoryGuard(cfg.memory, deck, controller.note_memory)

    background: list[Awaitable] = [
        _supervised("deck", lambda: deck.start()),
        _supervised("power", lambda: power.start()),
        _supervised("memory", lambda: guard.run()),
        _supervised("ticker", lambda: _ticker(controller)),
    ]
    return controller, background


async def _run_controller(
    controller: Controller, failures: list[str], cancel_all: Callable[[], None]
) -> None:
    """The controller is the only fatal task: if it dies, exit non-zero (systemd restarts us)."""
    try:
        await controller.run()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("controller died unexpectedly")
        failures.append("controller")
        cancel_all()


async def run_app(cfg: Config, dry_run: bool, record_dir: Path | None = None) -> int:
    """Run the controller until SIGTERM/SIGINT (exit 0) or a controller crash (exit 1)."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    controller, background = build_app(cfg, dry_run=dry_run, record_dir=record_dir)
    loop = asyncio.get_running_loop()
    failures: list[str] = []
    tasks: list[asyncio.Task] = []

    def cancel_all() -> None:
        for t in tasks:
            t.cancel()

    def on_stop_signal(name: str) -> None:
        log.info("received %s; shutting down (OBS stream is left untouched)", name)
        cancel_all()

    installed: list[signal.Signals] = []
    try:
        loop.add_signal_handler(
            signal.SIGUSR1, lambda: controller.sink(RetryRequested())
        )
        installed.append(signal.SIGUSR1)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, on_stop_signal, sig.name)
            installed.append(sig)

        tasks.append(
            loop.create_task(
                _run_controller(controller, failures, cancel_all), name="controller"
            )
        )
        for i, aw in enumerate(background):
            tasks.append(loop.create_task(aw, name=f"background-{i}"))
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # A task cancelled before its first step never awaited its coroutine.
        for aw in background:
            if inspect.iscoroutine(aw) and inspect.getcoroutinestate(aw) == inspect.CORO_CREATED:
                aw.close()
        for sig in installed:
            loop.remove_signal_handler(sig)
        await _stop_deck(controller)
    return 1 if failures else 0


async def _stop_deck(controller: Controller) -> None:
    """Release the Stream Deck HID device (best effort; never raises)."""
    stop = getattr(controller.deck, "stop", None)
    if stop is None:
        return
    try:
        await asyncio.wait_for(stop(), _DECK_STOP_TIMEOUT_S)
    except Exception:
        log.exception("stopping the deck adapter failed")


__all__ = ["NotWiredCapture", "NotWiredFacebook", "build_app", "run_app"]
