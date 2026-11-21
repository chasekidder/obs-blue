"""The stream-pilot controller state machine.

One press of the Stream Deck go-live key goes live; the power button ends the
stream and powers off. Facebook auto-ends a live when OBS sends Stop Streaming,
so ``obs.stop_stream()`` is only ever called from

* the end flow (power button or long-press while live),
* the setup flow (state ``SETUP`` and earlier, where no live exists).

Structure
---------
* ``handle(event)`` is the only entry point for events. It never blocks on a
  long operation: setup, go-live and end run as one background *flow* task
  (``self._flow``); starting a new flow cancels the old one. The OBS
  "restart loop" used after OBS gives up is a second, separate task
  (``self._restart``).
* Every state change goes through ``_transition`` which is synchronous (no
  awaits), so a state change can never be interleaved with another event.
* Flow tasks are always started via ``_start_flow`` with a coroutine
  *factory*, wrapped in ``_guarded`` so that a flow can never die silently
  (``StepFailed`` and unexpected exceptions both end in ``ERROR``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

from .comment_log import build_report
from .config import Config
from .events import (
    DeckPress,
    Event,
    ObsStreamState,
    PowerPress,
    RetryRequested,
    Tick,
)
from .persist import Saved, StateStore, StatusWriter
from .ports import (
    CameraPort,
    CommentCapturePort,
    DeckPort,
    FacebookPort,
    ObsPort,
    StepFailed,
    SystemPort,
)
from .states import LIVE_STATES, STATUS_FACES, KeyFace, State

log = logging.getLogger(__name__)

CONNECT_RETRY_S = 2.0
LOGIN_POLL_S = 5.0
STOP_POLL_S = 0.5
# Upper bound for awaits made inline from handle(): a hung adapter must never
# keep a queued PowerPress waiting.
INLINE_TIMEOUT_S = 5.0
# Upper bound for each adapter call in the end flow / power-off flow: a hung
# adapter must never keep system.poweroff() from running.
END_STEP_TIMEOUT_S = 15.0
# Upper bound for one capture re-attach (it may reconnect Chromium and reopen the
# comments popout); it runs in its own task, so it never blocks handle().
REATTACH_TIMEOUT_S = 90.0
# How long end/power/aclose wait for a cancelled re-attach to unwind before
# abandoning it: a hung adapter must never keep the end flow from starting.
REATTACH_CANCEL_WAIT_S = 3.0
# Re-attach attempts back off after failures: next try in min(MAX, BASE * 2**failures) s.
REATTACH_BACKOFF_BASE_S = 15.0
REATTACH_BACKOFF_MAX_S = 300.0
# Bound for open_comments / capture.start in the go-live and resume paths.
CAPTURE_STEP_TIMEOUT_S = 90.0

# States in which the retry key / go-live-key-as-retry must be ignored.
_NO_RETRY = frozenset({State.GOING_LIVE, State.LIVE, State.RECONNECTING, State.ENDING})

_FACE_RETRY = KeyFace("RETRY", "#333333")
_FACE_SPARE = KeyFace("", "#000000")
_FACE_AF_ON = KeyFace("AF\nON", "#1e9e3e")
_FACE_AF_OFF = KeyFace("AF\nOFF", "#555555")
_CAMERA_KEYS = {2: "main", 5: "jewelry"}
_CAMERA_FACES = {"main": "MAIN\nCAM", "jewelry": "JEWEL\nCAM"}
_CAMERA_IDLE_BG = "#333333"
_CAMERA_ACTIVE_BG = "#1f5fbf"


class Controller:
    def __init__(
        self,
        cfg: Config,
        *,
        obs: ObsPort,
        fb: FacebookPort,
        capture: CommentCapturePort,
        deck: DeckPort,
        system: SystemPort,
        camera: CameraPort,
        store: StateStore,
        status: StatusWriter,
        dry_run: bool = False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.cfg = cfg
        self.obs = obs
        self.fb = fb
        self.capture = capture
        self.deck = deck
        self.system = system
        self.camera = camera
        self.store = store
        self.status = status
        self.dry_run = dry_run
        # Injected monotonic clock; reserved for internal timing (all waits
        # currently count injected sleeps so they stay deterministic in tests).
        self.clock = clock
        self._sleep = sleep

        self.state: State = State.STARTING
        self.live_id: str | None = None
        self.stream_dir: Path | None = None
        self.last_error: str | None = None

        self._error_step: str | None = None
        self._since: float = time.time()
        self._memory: dict | None = None
        self._obs_ok = False
        self._fb_ok = False
        self._flow: asyncio.Task | None = None
        self._restart: asyncio.Task | None = None
        self._reattach: asyncio.Task | None = None
        self._reattach_failures = 0
        self._reattach_next_try = 0.0
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_camera: str | None = None
        self._autofocus: dict[str, bool] = {}
        # None: no end flow in progress; "end": long-press end flow before its
        # branch point; "power": a power-off is in progress (further presses ignored).
        self._end_mode: str | None = None
        self._power_after_end = False

    # ------------------------------------------------------------------ public

    def sink(self, event: Event) -> None:
        """Thread-safe enqueue (usable from the Stream Deck / evdev threads)."""
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or running is loop:
            # No loop captured yet means boot()/run() has not started. Adapters
            # (deck, power key) are only started after run(), so nothing can call
            # sink() from another thread here; put_nowait is safe (no getters yet).
            self._queue.put_nowait(event)
        else:
            loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def note_memory(self, sample: dict) -> None:
        """Record the latest memory-guard sample and rewrite status.json."""
        self._memory = sample
        self._write_status()

    async def boot(self) -> None:
        """Resume an interrupted live, or start the setup flow."""
        self._loop = asyncio.get_running_loop()
        self._show(0, self._face_for(self.state))
        self._show(1, _FACE_AF_OFF)
        self._show_camera_faces()
        self._show(3, _FACE_RETRY)
        self._show(4, _FACE_SPARE)

        saved = None
        try:
            saved = self.store.load()
        except Exception:
            log.exception("cannot load saved state")
        if (
            saved is not None
            and saved.state in {s.value for s in LIVE_STATES}
            and saved.live_id
        ):
            self.live_id = saved.live_id
            if saved.stream_dir:
                self.stream_dir = Path(saved.stream_dir)
            else:
                self.stream_dir = self.cfg.paths.streams_dir / f"resumed-{saved.live_id}"
            # RECONNECTING (a live state, same live_id/stream_dir persisted) while
            # we wait for OBS: retry/go-live keys are ignored, the power key ends
            # the stream properly, and a crash now still resumes next boot.
            self._transition(State.RECONNECTING)
            await self._start_flow(self._resume_flow)
            return

        self._transition(State.STARTING)
        await self._start_flow(lambda: self._setup_flow("STARTING"))

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            await self.boot()
            while True:
                await self.handle(await self._queue.get())
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Cancel background tasks (shutdown / test teardown)."""
        await self._cancel_bounded(self._reattach, REATTACH_CANCEL_WAIT_S)
        await self._cancel(self._restart)
        await self._cancel(self._flow)

    async def wait_idle(self) -> None:
        """Test helper: wait for the flow task and the restart loop to finish."""
        while True:
            pending = {
                t
                for t in (self._flow, self._restart, self._reattach)
                if t is not None and not t.done()
            }
            if not pending:
                return
            await asyncio.wait(pending)

    async def handle(self, event: Event) -> None:
        try:
            await self._dispatch(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("error handling %r", event)

    # ---------------------------------------------------------------- dispatch

    async def _dispatch(self, event: Event) -> None:
        if isinstance(event, DeckPress):
            await self._on_deck(event)
        elif isinstance(event, PowerPress):
            await self._on_power()
        elif isinstance(event, ObsStreamState):
            await self._on_obs(event)
        elif isinstance(event, RetryRequested):
            await self._retry()
        elif isinstance(event, Tick):
            await self._on_tick()

    async def _on_deck(self, ev: DeckPress) -> None:
        if ev.key == 0:
            if ev.long:
                if self.state in LIVE_STATES:
                    await self._begin_end(power_off=False)
                return
            if self.state == State.READY:
                await self._go_live_pressed()
            elif self.state == State.ERROR:
                await self._retry()
        elif ev.key == 3:
            await self._retry()
        elif ev.key in _CAMERA_KEYS:
            await self._camera_press(_CAMERA_KEYS[ev.key])
        elif ev.key == 1:
            await self._autofocus_press()

    async def _retry(self) -> None:
        if self.state in _NO_RETRY:
            log.info("retry ignored in %s", self.state)
            return
        await self._start_flow(lambda: self._setup_flow("LOGIN"))

    async def _go_live_pressed(self) -> None:
        if self.dry_run:
            log.info("dry-run: would go live")
            return
        await self._start_flow(self._go_live_flow, enter=State.GOING_LIVE)

    async def _on_power(self) -> None:
        if self.state == State.ENDING:
            if self._end_mode == "end":
                # Long-press end flow in progress: power off when it reaches its
                # branch point instead of returning to setup.
                self._power_after_end = True
            elif self._end_mode is None:
                # Past the end flow's branch point (setup after a long-press end,
                # no live exists any more): shut down like any non-live state.
                await self._start_flow(self._shutdown_flow, enter=State.ENDING)
            # "power": a power-off is already running; ignore.
            return
        if self.state in LIVE_STATES:
            await self._begin_end(power_off=True)
        else:
            await self._start_flow(self._shutdown_flow, enter=State.ENDING)

    async def _begin_end(self, *, power_off: bool) -> None:
        await self._cancel_bounded(self._reattach, REATTACH_CANCEL_WAIT_S)
        await self._cancel(self._restart)
        self._power_after_end = False
        self._end_mode = "power" if power_off else "end"
        await self._start_flow(
            lambda: self._end_flow(power_off=power_off), enter=State.ENDING
        )

    async def _on_obs(self, ev: ObsStreamState) -> None:
        if self.state not in LIVE_STATES:
            return
        if ev.reconnecting:
            if self.state == State.LIVE:
                self._transition(State.RECONNECTING)
        elif ev.active:
            if self.state == State.RECONNECTING:
                await self._cancel(self._restart)
                self._transition(State.LIVE)
        else:
            self._obs_gave_up()

    async def _on_tick(self) -> None:
        self._write_status()
        if self.state == State.READY and self._flow_idle():
            try:
                lost = await self._bounded("preview_lost", self.fb.preview_lost())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("preview_lost failed: %r", exc)
                return
            # Re-check: the world may have moved while we awaited.
            if lost and self.state == State.READY and self._flow_idle():
                log.warning("facebook preview lost; re-running setup")
                await self._start_flow(
                    lambda: self._setup_flow("SETUP"), enter=State.SETUP
                )
        elif self.state in LIVE_STATES:
            self._maybe_reattach_capture()
            if self.state == State.LIVE:
                await self._poll_obs_while_live()

    async def _poll_obs_while_live(self) -> None:
        try:
            active = await self._bounded("stream_active", self.obs.stream_active())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("stream_active poll failed: %r", exc)
            return
        if not active and self.state == State.LIVE:
            log.warning("OBS stream inactive while LIVE; restarting it")
            self._obs_gave_up()

    def _maybe_reattach_capture(self) -> None:
        """Re-run ``capture.start`` when the capture reports itself unhealthy.

        Only for capture objects that provide ``healthy()`` (the browser
        adapter: its popout page closed, e.g. a Chromium crash/restart). The
        attempt runs in its own task (it may reconnect and reopen the popout),
        at most one at a time, so a Tick can never be held up by it.
        """
        probe = getattr(self.capture, "healthy", None)
        if probe is None or not self._flow_idle():
            return
        if self._reattach is not None and not self._reattach.done():
            return
        live_id, stream_dir = self.live_id, self.stream_dir
        if live_id is None or stream_dir is None:
            return
        if self.clock() < self._reattach_next_try:
            return
        try:
            if probe():
                return
        except Exception:
            log.exception("capture.healthy failed (ignored)")
            return
        log.warning("comment capture is not attached; re-attaching to live %s", live_id)
        self._reattach = asyncio.get_running_loop().create_task(
            self._reattach_capture(live_id, stream_dir)
        )

    async def _reattach_capture(self, live_id: str, stream_dir: Path) -> None:
        try:
            await asyncio.wait_for(self.capture.start(live_id, stream_dir), REATTACH_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            log.warning("capture.reattach timed out after %.1fs (continuing)", REATTACH_TIMEOUT_S)
            self._reattach_failed()
        except Exception:
            log.exception("capture.reattach failed (continuing)")
            self._reattach_failed()
        else:
            self._reset_reattach_backoff()

    def _reset_reattach_backoff(self) -> None:
        """Backoff is per stream: a new (or resumed, or ended) stream starts clean."""
        self._reattach_failures = 0
        self._reattach_next_try = 0.0

    def _reattach_failed(self) -> None:
        self._reattach_failures += 1
        delay = min(
            REATTACH_BACKOFF_MAX_S, REATTACH_BACKOFF_BASE_S * 2**self._reattach_failures
        )
        self._reattach_next_try = self.clock() + delay
        log.info("next capture re-attach in %.0fs", delay)

    @staticmethod
    async def _bounded(name: str, aw: Awaitable):
        """Await ``aw`` for at most INLINE_TIMEOUT_S (TimeoutError on expiry)."""
        try:
            return await asyncio.wait_for(aw, INLINE_TIMEOUT_S)
        except TimeoutError:
            log.warning("%s timed out after %.1fs", name, INLINE_TIMEOUT_S)
            raise

    # ------------------------------------------------------- state/persistence

    def _face_for(self, state: State) -> KeyFace:
        if state == State.ERROR and self._error_step is not None:
            base = STATUS_FACES[State.ERROR]
            return dataclasses.replace(base, label="ERR\n" + self._error_step[:6].upper())
        return STATUS_FACES[state]

    def _transition(self, state: State) -> None:
        """Enter ``state``: persist, write status, update key 0. Never awaits."""
        self.state = state
        self._since = time.time()
        log.info("state -> %s", state.value)
        try:
            self.store.save(
                Saved(
                    state.value,
                    self.live_id,
                    str(self.stream_dir) if self.stream_dir else None,
                )
            )
        except Exception:
            log.exception("cannot save state")
        self._write_status()
        self._show(0, self._face_for(state))

    def _enter(self, state: State) -> None:
        if self.state != state:
            self._transition(state)

    def _write_status(self) -> None:
        try:
            self.status.write(
                state=self.state.value,
                since=self._since,
                last_error=self.last_error,
                live_id=self.live_id,
                memory=self._memory,
            )
        except Exception:
            log.exception("cannot write status")

    def _show(self, key: int, face: KeyFace) -> None:
        try:
            self.deck.show(key, face)
        except Exception:
            log.exception("cannot update deck key %d", key)

    def _enter_error(self, step: str, detail: str) -> None:
        self.last_error = f"{step}: {detail}"
        self._error_step = step
        log.error("ERROR %s", self.last_error)
        self._transition(State.ERROR)

    async def _fail(self, step: str, detail: str) -> None:
        directory = self.cfg.paths.failures_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{step}"
        try:
            await self.fb.save_failure(step, directory)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("save_failure(%s) failed", step)
        self._enter_error(step, detail)

    # ------------------------------------------------------------- flow tasks

    def _flow_idle(self) -> bool:
        return self._flow is None or self._flow.done()

    @staticmethod
    async def _cancel_bounded(task: asyncio.Task | None, timeout: float) -> None:
        """Cancel ``task`` but wait at most ``timeout`` s for it to unwind; then abandon it."""
        if task is None or task.done():
            return
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            log.warning("a cancelled background task did not stop within %.1fs; abandoning it", timeout)
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            return
        if not task.cancelled():
            task.exception()  # mark retrieved

    @staticmethod
    async def _cancel(task: asyncio.Task | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        # asyncio.wait neither raises the task's CancelledError nor swallows our own.
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # mark retrieved

    async def _start_flow(
        self, factory: Callable[[], Awaitable[None]], *, enter: State | None = None
    ) -> None:
        await self._cancel(self._flow)
        if enter is not None:
            self._transition(enter)  # synchronous with the task creation below
        self._flow = asyncio.get_running_loop().create_task(self._guarded(factory))

    async def _guarded(self, factory: Callable[[], Awaitable[None]]) -> None:
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except StepFailed as exc:
            await self._fail(exc.step, exc.detail)
        except Exception as exc:
            log.exception("flow crashed")
            await self._fail("internal", repr(exc))

    # -------------------------------------------------------------- setup flow

    async def _ensure_connected(self, *, announce: bool = True) -> None:
        """Connect OBS and Chromium, retrying every 2 s until both succeed.

        ``announce=False`` stays in the current state (used when resuming a live,
        where leaving RECONNECTING would drop the live protections).
        """
        if self._obs_ok and self._fb_ok:
            return
        if announce:
            self._enter(State.STARTING)
        while True:
            if not self._obs_ok:
                try:
                    await self.obs.connect()
                    self._obs_ok = True
                    await self._sync_camera_faces()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.info("OBS not ready: %r", exc)
            if not self._fb_ok:
                try:
                    await self.fb.connect()
                    self._fb_ok = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.info("Chromium not ready: %r", exc)
            if self._obs_ok and self._fb_ok:
                return
            await self._sleep(CONNECT_RETRY_S)

    async def _setup_flow(self, start_at: str) -> None:
        if start_at == "STARTING":
            self._enter(State.STARTING)
        await self._ensure_connected()

        if start_at in ("STARTING", "LOGIN"):
            missing = self.cfg.missing_site_values()
            if missing:
                self._enter_error("config", "missing site values: " + ", ".join(missing))
                return

            if not await self._reachable():
                self._enter(State.NO_NET)
                while True:
                    await self._sleep(self.cfg.timeouts.net_retry_s)
                    if await self._reachable():
                        break

            await self._login_phase()

        if not await self._setup_phase():
            return  # a live is running: leave it (and its state) alone
        self._transition(State.READY)

    async def _reachable(self) -> bool:
        try:
            return bool(await self.fb.reachable())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("reachability check failed: %r", exc)
            return False

    async def _login_phase(self) -> None:
        self._enter(State.LOGIN)
        attempts = max(1, self.cfg.timeouts.attempts)
        failures = 0
        while True:
            try:
                if await self.fb.logged_in():
                    return
            except StepFailed as exc:
                failures += 1
                log.warning("login check failed (%d/%d): %s", failures, attempts, exc)
                if failures >= attempts:
                    raise
            else:
                failures = 0
            await self._sleep(LOGIN_POLL_S)

    def _setup_blocked_by_live(self) -> bool:
        """True (and logged) if a live is running, so setup must not touch anything.

        This is deliberately not a StepFailed: that would be retried and could
        eventually put a running live into ERROR.
        """
        if self.state in LIVE_STATES:
            log.error("refusing to run setup: state is %s (a live is running)", self.state)
            return True
        return False

    async def _setup_phase(self) -> bool:
        """Returns True when setup completed, False when skipped because live."""
        if self._setup_blocked_by_live():
            return False
        self._enter(State.SETUP)
        attempts = max(1, self.cfg.timeouts.attempts)
        t = self.cfg.timeouts
        for attempt in range(1, attempts + 1):
            if self._setup_blocked_by_live():
                return False
            try:
                target = await self.fb.setup_live()
                await self.obs.set_stream_target(target)
                if await self.obs.stream_active():
                    # SETUP: no live exists (the live only starts with go-live),
                    # so replacing the pre-roll stream is safe. Local guard: never
                    # stop the stream if we somehow are in a live state.
                    if self._setup_blocked_by_live():
                        return False  # never touch a live's stream (nor ERROR it)
                    await self.obs.stop_stream()
                    await self._wait_stream_stopped()
                await self.obs.start_stream()
                await self.fb.wait_preview_ready(t.preview_s)
                return True
            except StepFailed as exc:
                log.warning("setup attempt %d/%d failed: %s", attempt, attempts, exc)
                if attempt >= attempts:
                    raise
        return True  # unreachable: the loop returns or raises

    async def _wait_stream_stopped(self) -> None:
        waited = 0.0
        stop_s = self.cfg.timeouts.stop_s
        while True:
            try:
                active = await asyncio.wait_for(
                    self.obs.stream_active(), END_STEP_TIMEOUT_S
                )
                if not active:
                    return
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # A hung poll counts as waiting that long, so a hung OBS cannot
                # stretch the stop wait beyond ~stop_s (+ one timeout).
                log.warning("stream_active poll timed out after %.1fs", END_STEP_TIMEOUT_S)
                waited += END_STEP_TIMEOUT_S
            except Exception as exc:
                log.warning("stream_active poll failed: %r", exc)
            if waited >= stop_s:
                log.warning("OBS still reports streaming after %.1fs", stop_s)
                return
            await self._sleep(STOP_POLL_S)
            waited += STOP_POLL_S

    # ----------------------------------------------------------------- resume

    async def _resume_flow(self) -> None:
        live_id = self.live_id
        assert live_id is not None and self.stream_dir is not None
        self._reset_reattach_backoff()
        await self._ensure_connected(announce=False)
        while True:
            try:
                active = await self.obs.stream_active()
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("resume: stream_active failed: %r", exc)
                await self._sleep(CONNECT_RETRY_S)
        if not active:
            log.warning("resume: saved live %s but OBS is not streaming; starting setup", live_id)
            await self._cancel(self._restart)
            self.live_id = None
            self.stream_dir = None
            await self._setup_flow("STARTING")
            return
        self._mkdir(self.stream_dir)
        await self._best_effort(
            "resume: capture.start",
            self.capture.start(live_id, self.stream_dir),
            CAPTURE_STEP_TIMEOUT_S,
        )
        self._transition(State.LIVE)

    @staticmethod
    def _mkdir(path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("cannot create %s", path)

    # ---------------------------------------------------------------- go live

    async def _go_live_flow(self) -> None:
        self._enter(State.GOING_LIVE)
        # Exactly one attempt: a second click could create a duplicate live.
        live_id = await self.fb.go_live(self.cfg.timeouts.live_confirm_s)
        stream_dir = (
            self.cfg.paths.streams_dir / f"{time.strftime('%Y-%m-%d_%H%M')}-{live_id}"
        )
        self._mkdir(stream_dir)
        self.live_id = live_id
        self.stream_dir = stream_dir
        self._reset_reattach_backoff()
        # The stream is live now: persist LIVE (with live_id) before anything else,
        # so a crash resumes instead of re-running setup, and a power/long-press
        # during the steps below runs the normal live end flow (cancelling this flow).
        self._transition(State.LIVE)
        # Best effort: nothing below may turn into ERROR.
        await self._best_effort(
            "open_comments", self.fb.open_comments(live_id), CAPTURE_STEP_TIMEOUT_S
        )
        await self._best_effort(
            "capture.start", self.capture.start(live_id, stream_dir), CAPTURE_STEP_TIMEOUT_S
        )
        await self._best_effort("park_dashboard", self.fb.park_dashboard())

    @staticmethod
    async def _best_effort(
        name: str, aw: Awaitable[object], timeout: float | None = None
    ) -> None:
        try:
            if timeout is None:
                await aw
            else:
                await asyncio.wait_for(aw, timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            log.warning("%s timed out after %.1fs (continuing)", name, timeout)
        except Exception:
            log.exception("%s failed (continuing)", name)

    async def _bounded_step(self, name: str, aw: Awaitable[object]) -> None:
        """Best-effort end-flow step, at most END_STEP_TIMEOUT_S (read at call time)."""
        await self._best_effort(name, aw, END_STEP_TIMEOUT_S)

    # -------------------------------------------------------- OBS restart loop

    def _obs_gave_up(self) -> None:
        if self.state == State.LIVE:
            self._transition(State.RECONNECTING)
        if self._restart is None or self._restart.done():
            self._restart = asyncio.get_running_loop().create_task(self._restart_loop())

    async def _restart_loop(self) -> None:
        # Facebook holds a dropped live open, so keep restarting with the same
        # key. Never stop_stream here.
        while True:
            try:
                await self.obs.start_stream()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("restart start_stream failed: %r", exc)
            await self._sleep(self.cfg.timeouts.reconnect_retry_s)
            try:
                active = await self.obs.stream_active()
            except asyncio.CancelledError:
                raise
            except Exception:
                active = False
            if active:
                if self.state == State.RECONNECTING:
                    self._transition(State.LIVE)
                return

    # ------------------------------------------------------------------- end

    async def _end_flow(self, *, power_off: bool) -> None:
        self._reset_reattach_backoff()
        await self._bounded_step("stop_stream", self.obs.stop_stream())
        await self._wait_stream_stopped()

        await self._sleep(self.cfg.timeouts.fb_end_wait_s)
        await self._bounded_step("capture.stop", self.capture.stop())
        self._write_report()

        # Branch point (synchronous with the flag check, so a PowerPress either
        # sets the flag before it or is handled as a plain shutdown after it).
        if power_off or self._power_after_end:
            self._end_mode = "power"
            await self._power_steps()
            return
        self._end_mode = None
        self.live_id = None
        self.stream_dir = None
        await self._setup_flow("LOGIN")

    def _write_report(self) -> None:
        if self.stream_dir is None:
            return
        comments = self.stream_dir / "comments.jsonl"
        try:
            if comments.exists():
                (self.stream_dir / "report.txt").write_text(
                    build_report(comments), encoding="utf-8"
                )
        except Exception:
            log.exception("cannot write report")

    async def _shutdown_flow(self) -> None:
        self._end_mode = "power"
        await self._power_steps()

    async def _power_steps(self) -> None:
        await self._bounded_step("close_browser", self.fb.close_browser())
        await self._bounded_step(
            "stop obs.service", self.system.stop_user_unit("obs.service")
        )
        await self._best_effort("poweroff", self.system.poweroff())

    # ------------------------------------------------------------ deck cameras

    def _show_camera_faces(self) -> None:
        for key, cam in _CAMERA_KEYS.items():
            bg = _CAMERA_ACTIVE_BG if cam == self._active_camera else _CAMERA_IDLE_BG
            self._show(key, KeyFace(_CAMERA_FACES[cam], bg))

    def _show_autofocus(self) -> None:
        on = self._active_camera is not None and self._autofocus.get(self._active_camera, False)
        self._show(1, _FACE_AF_ON if on else _FACE_AF_OFF)

    async def _camera_press(self, cam: str) -> None:
        cfg = self.cfg.cameras.get(cam)
        if cfg is None:
            log.warning("no camera %r configured", cam)
            return
        try:
            await self._bounded("set_scene", self.obs.set_scene(cfg.scene))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("set_scene(%s) failed", cfg.scene)
            return
        self._active_camera = cam
        try:
            await self._bounded(
                "set_autofocus", self.camera.set_autofocus(cfg.device, True)
            )
            self._autofocus[cam] = True
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("autofocus on %s failed", cfg.device)
        self._show_camera_faces()
        self._show_autofocus()

    async def _autofocus_press(self) -> None:
        cam = self._active_camera
        if cam is None:
            cam = await self._camera_from_scene()
        cfg = self.cfg.cameras.get(cam)
        if cfg is None:
            log.warning("no camera %r configured", cam)
            return
        want = not self._autofocus.get(cam, False)
        try:
            await self._bounded(
                "set_autofocus", self.camera.set_autofocus(cfg.device, want)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("autofocus toggle on %s failed", cfg.device)
            return
        self._active_camera = cam
        self._autofocus[cam] = want
        self._show_camera_faces()
        self._show_autofocus()

    async def _sync_camera_faces(self) -> None:
        """Show the camera OBS is on and its real autofocus state.

        Cameras keep their own control values (a C920 powers up with continuous
        autofocus on), so assuming "off" would make the first AF press a no-op.
        """
        try:
            scene = await self._bounded("current_scene", self.obs.current_scene())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("current_scene failed: %r", exc)
            return
        cam = next((n for n, c in self.cfg.cameras.items() if c.scene == scene), None)
        if cam is None:
            return
        try:
            on = await self._bounded(
                "get_autofocus", self.camera.get_autofocus(self.cfg.cameras[cam].device)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("get_autofocus failed: %r", exc)
            on = None
        self._active_camera = cam
        if on is not None:
            self._autofocus[cam] = on
        self._show_camera_faces()
        self._show_autofocus()

    async def _camera_from_scene(self) -> str:
        try:
            scene = await self._bounded("current_scene", self.obs.current_scene())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("current_scene failed: %r", exc)
            return "main"
        for name, cam in self.cfg.cameras.items():
            if cam.scene == scene:
                return name
        return "main"
