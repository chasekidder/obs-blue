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
from typing import Awaitable, Callable, Sequence

from .comment_log import build_report
from .config import Config
from .events import (
    DeckPress,
    DeckDown,
    EndRequested,
    Event,
    ObsConnected,
    ObsStreamState,
    PauseRequested,
    PowerPress,
    ProgramSceneChanged,
    RetryRequested,
    SsnStalled,
    Tick,
)
from .features import Feature, run_hook
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
from .obs import CODE_RESOURCE_NOT_FOUND, ObsRequestFailed
from .scenes import obs_scene_names, resolve_scene
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
# The whole end flow (power press or long-press to power-off) must finish inside
# the power guard's window: it powers off 15 s after a press unless status.json
# says ENDING, then allows 90 s more. Steps after the stream stopped get what is
# left of this budget; closing Chromium and stopping OBS always get at least
# END_TAIL_MIN_S each.
END_FLOW_BUDGET_S = 70.0
END_TAIL_RESERVE_S = 10.0
END_TAIL_MIN_S = 2.0
# Caps for feature work in the end flow (within the budget above).
END_HOOK_TIMEOUT_S = 12.0
# The outro card (branding) before StopStream: its longest allowed outro_s plus a margin.
END_OUTRO_CAP_S = 17.0
# Upper bound for one capture re-attach (it may reconnect Chromium and reopen the
# comments popout); it runs in its own task, so it never blocks handle().
REATTACH_TIMEOUT_S = 90.0
# How long end/power/aclose wait for a cancelled re-attach to unwind before
# abandoning it: a hung adapter must never keep the end flow from starting.
REATTACH_CANCEL_WAIT_S = 3.0
# Re-attach attempts back off after failures: next try in min(MAX, BASE * 2**failures) s.
REATTACH_BACKOFF_BASE_S = 15.0
REATTACH_BACKOFF_MAX_S = 300.0
# The comments column can load without Facebook's live updates (seen 2026-11-25:
# new comments never arrived until a reload), so while LIVE the capture compares
# it with Facebook this often (capture.check_fresh), bounded by the timeout.
FRESH_CHECK_S = 90.0
FRESH_CHECK_TIMEOUT_S = 60.0
# A live ended on Facebook (e.g. from the host's phone) drops OBS's connection and
# leaves it reconnecting forever. After this long in RECONNECTING, Facebook is asked
# (capture.live_ended: the column, else a fresh copy of the dashboard) at most every
# ENDED_EVERY_S; if it says the live ended, it is ended here too.
ENDED_AFTER_S = 20.0
ENDED_EVERY_S = 30.0
# Bound for open_comments / capture.start in the go-live and resume paths.
CAPTURE_STEP_TIMEOUT_S = 90.0
# Bound for having SSN reopen its background tab (runs in its own task when self-healing).
SSN_REFRESH_TIMEOUT_S = 30.0
# SSN repeats its warning about once a minute while the tab is stuck; one refresh per warning.
SSN_HEAL_MIN_INTERVAL_S = 60.0
# While waiting for Facebook's preview, how often OBS's pre-roll is checked. Facebook
# can refuse an RTMP connect made right after the previous one with the same
# persistent key stopped; OBS then just stops, so the pre-roll is restarted.
PREROLL_CHECK_S = 5.0
# Facebook refuses the persistent key's RTMP connects for about 60 s after a stream
# stopped (measured on the stream PC), and OBS opens a modal "Failed to connect"
# dialog for every refusal, so a new pre-roll starts no earlier than this after a stop.
PREROLL_RECONNECT_WAIT_S = 65.0

# States in which the retry key / go-live-key-as-retry must be ignored.
_NO_RETRY = frozenset({State.GOING_LIVE, State.LIVE, State.RECONNECTING, State.ENDING})
# A live ends on a second status-key press within this long of the first.
END_CONFIRM_S = 5.0
# ERROR is retried by itself this often (the key keeps showing the failed step).
ERROR_RETRY_S = 120.0
# How long a key shows that a press did nothing (BUSY, NO SCENE).
FLASH_S = 2.0
# Holding RETRY this long pauses (or resumes) stream-pilot; the key counts down from
# PAUSE_COUNTDOWN_FROM_S into the hold, so a short press shows no countdown.
PAUSE_HOLD_S = 3.0
PAUSE_COUNTDOWN_FROM_S = 0.5
_PAUSED_BG = "#8a6d00"
_PAUSED_LABELS = {
    State.STARTING: "BOOT", State.NO_NET: "NO NET", State.LOGIN: "LOG IN", State.SETUP: "SETUP",
    State.READY: "READY", State.GOING_LIVE: "GOING", State.LIVE: "LIVE",
    State.RECONNECTING: "RECON", State.ENDING: "ENDING", State.ERROR: "ERROR",
}
# Corrective work (setup) that pause stops; go-live and end flows are the host's own.
_SETUP_STATES = frozenset({State.STARTING, State.NO_NET, State.LOGIN, State.SETUP})

_FACE_RETRY = KeyFace("RETRY", "#333333")
_FACE_PRESS_AGAIN = KeyFace("PRESS\nAGAIN", "#c0141c", blink=True)
_FACE_BUSY = KeyFace("BUSY", "#555555")
_FACE_NO_SCENE = KeyFace("NO\nSCENE", "#e67e00")
_FACE_SPARE = KeyFace("", "#000000")
_FACE_AF_ON = KeyFace("AF\nON", "#1e9e3e")
_FACE_AF_OFF = KeyFace("AF\nOFF", "#555555")
_FACE_MACRO = KeyFace("MACRO", "#7b3fbf")
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
        features: Sequence[Feature] = (),
        pause_flag: Path | None = None,
    ):
        self.cfg = cfg
        self.features = list(features)
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
        self._stream_stopped_at: float | None = None  # clock() when OBS last stopped streaming
        self._flow: asyncio.Task | None = None
        self._restart: asyncio.Task | None = None
        self._reattach: asyncio.Task | None = None
        self._reattach_failures = 0
        self._reattach_next_try = 0.0
        self._fresh: asyncio.Task | None = None
        self._ended: asyncio.Task | None = None
        self._recon_at: float | None = None  # clock() when RECONNECTING was entered
        self._ended_checked_at: float | None = None
        self._fresh_next: float | None = None  # clock() of the next check; None: not scheduled
        self._ssn_heal: asyncio.Task | None = None
        self._ssn_healed_at: float | None = None
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_camera: str | None = None
        self._autofocus: dict[str, bool] = {}
        self._macro: set[str] = set()  # cameras put in macro by the autofocus key
        # None: no end flow in progress; "end": long-press end flow before its
        # branch point; "power": a power-off is in progress (further presses ignored).
        self._end_mode: str | None = None
        self._power_after_end = False
        self._feature_tasks: set[asyncio.Task] = set()
        self._obs_sync: asyncio.Task | None = None
        self._obs_sync_again = False
        self._go_live_pressed_at: float | None = None
        self._outro = False
        self._end_deadline: float | None = None  # clock() by which the end flow must be done
        self._end_armed_at: float | None = None  # clock() of an unconfirmed end press
        # An end asked for while GOING_LIVE ("end" or "power"): run once the live exists.
        self._end_after_live: str | None = None
        self._obs_retrying = False  # OBS reported RECONNECTING since the stream last started
        self._error_at: float | None = None
        self._flash_handles: dict[int, asyncio.TimerHandle] = {}
        self._scene_alias: dict[str, str] = {}  # configured camera scene -> OBS's name
        # Paused: stream-pilot changes nothing by itself (no setup, restarts, reloads,
        # window moves) until resumed. Kept in a runtime file: a stream-pilot restart
        # stays paused, a reboot does not.
        self._pause_flag = pause_flag
        self._paused = bool(pause_flag is not None and pause_flag.exists())
        self._hold_handles: list[asyncio.TimerHandle] = []
        self._hold_fired = False

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
        self._spawn_feature_task(self._restore_disabled_features())
        self._spawn_feature_task(self._feature_hook("started"))

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

        self._spawn_feature_task(self._feature_hook("not_live"))
        self._transition(State.STARTING)
        if self._paused:
            log.warning("started paused: no setup until resumed (RETRY, or stream-pilot pause)")
            self._spawn_feature_task(self._best_effort(
                "window layout off", self.system.set_window_layout(False), INLINE_TIMEOUT_S))
            return
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
        await self._cancel_bounded(self._fresh, REATTACH_CANCEL_WAIT_S)
        await self._cancel_bounded(self._ended, REATTACH_CANCEL_WAIT_S)
        await self._cancel(self._ssn_heal)
        await self._cancel(self._restart)
        await self._cancel(self._flow)
        for task in list(self._feature_tasks):
            await self._cancel_bounded(task, REATTACH_CANCEL_WAIT_S)

    async def wait_idle(self) -> None:
        """Test helper: wait for the flow task and the restart loop to finish."""
        while True:
            pending = {
                t
                for t in (self._flow, self._restart, self._reattach, self._fresh, self._ended,
                          self._ssn_heal,
                          *self._feature_tasks)
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
        elif isinstance(event, EndRequested):
            await self._end_requested()
        elif isinstance(event, ProgramSceneChanged):
            self._on_program_scene(event.name)
        elif isinstance(event, DeckDown):
            self._on_deck_down(event.key)
        elif isinstance(event, PauseRequested):
            await self._set_paused(not self._paused)
        elif isinstance(event, Tick):
            await self._on_tick()
        elif isinstance(event, SsnStalled):
            self._on_ssn_stalled()
        elif isinstance(event, ObsConnected):
            self._on_obs_connected()

    # ---------------------------------------------------------------- features

    def _spawn_feature_task(self, aw: Awaitable[None]) -> None:
        """Run feature work in the background (never inline in handle() or a flow)."""
        task = asyncio.get_running_loop().create_task(aw)
        self._feature_tasks.add(task)
        task.add_done_callback(self._feature_tasks.discard)

    async def _restore_disabled_features(self) -> None:
        """At boot; features whose restore needs OBS get it on the OBS connect instead."""
        for f in self.features:
            if not f.enabled and not f.restore_needs_obs:
                await run_hook(f, "restore", f.restore)

    def _on_obs_connected(self) -> None:
        """Apply every feature's OBS items; one sync at a time, re-run if OBS reconnects meanwhile."""
        self._spawn_feature_task(self._refresh_scene_alias())
        if self._obs_sync is not None and not self._obs_sync.done():
            self._obs_sync_again = True
            return
        self._obs_sync = asyncio.get_running_loop().create_task(self._sync_features_with_obs())
        self._feature_tasks.add(self._obs_sync)
        self._obs_sync.add_done_callback(self._feature_tasks.discard)

    async def _sync_features_with_obs(self) -> None:
        while True:
            self._obs_sync_again = False
            for f in self.features:
                if f.enabled:
                    await run_hook(f, "obs_connected", f.obs_connected)
                else:
                    await run_hook(f, "restore", f.restore)
            if not self._obs_sync_again:
                return

    async def _feature_claims(self, hook: str, *args: object) -> bool:
        """True if an enabled feature handled ``hook(*args)`` itself (bounded, best effort)."""
        for f in self.features:
            fn = getattr(f, hook, None)
            if not f.enabled or fn is None:
                continue
            try:
                if await asyncio.wait_for(fn(*args), INLINE_TIMEOUT_S):
                    return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("feature %s: %s failed (continuing): %r", f.name, hook, exc)
        return False

    async def _feature_hook(
        self, hook: str, *args: object, timeout: float | None = None
    ) -> None:
        """Call ``hook(*args)`` on every enabled feature that has it, best effort.

        ``timeout`` bounds each call (default: features.HOOK_TIMEOUT_S).
        """
        for f in self.features:
            fn = getattr(f, hook, None)
            if not f.enabled or fn is None:
                continue
            if timeout is None:
                await run_hook(f, hook, lambda: fn(*args))
            else:
                await run_hook(f, hook, lambda: fn(*args), timeout=timeout)

    async def _on_deck(self, ev: DeckPress) -> None:
        if ev.key == 0:
            if self.state == State.READY and not ev.long:
                await self._go_live_pressed()
            elif self.state == State.ERROR:
                await self._retry()
            elif self.state in LIVE_STATES or self.state == State.GOING_LIVE:
                await self._end_press()
        elif ev.key == 3:
            self._cancel_hold()
            if self._hold_fired:
                self._hold_fired = False  # the hold already paused / resumed
                self._show(3, _FACE_RETRY)
                return
            self._show(3, _FACE_RETRY)
            if self._paused:
                await self._set_paused(False)
            else:
                await self._retry()
        elif ev.key in _CAMERA_KEYS:
            if self.state == State.ENDING:
                log.info("camera key ignored while ending (the outro is on air)")
                return
            await self._camera_press(_CAMERA_KEYS[ev.key])
        elif ev.key == 1:
            await self._autofocus_press()

    async def _retry(self) -> None:
        if self.state in _NO_RETRY:
            log.info("retry ignored in %s", self.state)
            self._flash(3, _FACE_BUSY, _FACE_RETRY)
            return
        await self._start_flow(lambda: self._setup_flow("LOGIN"), enter=State.STARTING)

    # ------------------------------------------------------------- pause

    def _on_deck_down(self, key: int) -> None:
        if key != 3 or self._loop is None:
            return
        self._cancel_hold()
        self._hold_fired = False
        word = "RESUME" if self._paused else "PAUSE"
        steps = [(min(PAUSE_COUNTDOWN_FROM_S, PAUSE_HOLD_S / 6), 3),
                 (PAUSE_HOLD_S / 3, 2), (PAUSE_HOLD_S * 2 / 3, 1)]
        for at, n in steps:
            face = KeyFace(f"{word}\n{n}", _PAUSED_BG)
            self._hold_handles.append(self._loop.call_later(at, self._show, 3, face))
        self._hold_handles.append(self._loop.call_later(PAUSE_HOLD_S, self._hold_reached))

    def _cancel_hold(self) -> None:
        for h in self._hold_handles:
            h.cancel()
        self._hold_handles.clear()

    def _hold_reached(self) -> None:
        self._hold_handles.clear()
        self._hold_fired = True
        self.sink(PauseRequested())

    async def _set_paused(self, on: bool) -> None:
        if on == self._paused:
            return
        self._paused = on
        if self._pause_flag is not None:
            try:
                if on:
                    self._pause_flag.parent.mkdir(parents=True, exist_ok=True)
                    self._pause_flag.touch()
                else:
                    self._pause_flag.unlink(missing_ok=True)
            except OSError:
                log.exception("cannot record the pause in %s", self._pause_flag)
        self._show(3, _FACE_RETRY)
        if on:
            log.warning("paused: stream-pilot changes nothing by itself until resumed (state %s)",
                        self.state)
            if self.state in _SETUP_STATES:
                await self._cancel(self._flow)
            await self._cancel(self._restart)
            await self._cancel_bounded(self._reattach, REATTACH_CANCEL_WAIT_S)
            await self._cancel_bounded(self._fresh, REATTACH_CANCEL_WAIT_S)
            await self._best_effort("window layout off", self.system.set_window_layout(False),
                                    INLINE_TIMEOUT_S)
        else:
            log.warning("resumed: bringing %s back to a known state", self.state)
            await self._best_effort("window layout on", self.system.set_window_layout(True),
                                    INLINE_TIMEOUT_S)
        self._show(0, self._face_for(self.state))
        self._write_status()
        if not on:
            await self._reconcile()

    async def _reconcile(self) -> None:
        """After a pause: setup again where it was stopped, end a live whose stream is gone."""
        if self.state in _SETUP_STATES or self.state == State.ERROR:
            await self._retry()
        elif self.state in LIVE_STATES and self._flow_idle():
            try:
                active = await self._bounded("stream_active", self.obs.stream_active())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("resume: stream_active failed: %r", exc)
                return
            if not active:
                log.warning("resume: OBS is not streaming the live; ending it")
                await self._begin_end(power_off=False, outro=False)
        # READY and LIVE otherwise: the next Tick checks the pre-roll / the column.

    # ------------------------------------------------------------- ending

    async def _end_press(self) -> None:
        """Status key while GOING_LIVE/LIVE/RECONNECTING: the first press arms, a second
        one within END_CONFIRM_S ends the live (so a stray press never ends a sale)."""
        now = self.clock()
        armed = self._end_armed_at
        if armed is None or now - armed > END_CONFIRM_S:
            self._end_armed_at = now
            log.info("status key pressed while %s: press again within %.0fs to end",
                     self.state, END_CONFIRM_S)
            self._show(0, _FACE_PRESS_AGAIN)
            if self._loop is not None:
                self._loop.call_later(END_CONFIRM_S + 0.05, self._disarm_end_if_expired)
            return
        self._end_armed_at = None
        log.info("status key pressed again: ending the live")
        await self._end_requested()

    def _disarm_end_if_expired(self) -> None:
        armed = self._end_armed_at
        if armed is None or self.clock() - armed < END_CONFIRM_S:
            return
        self._end_armed_at = None
        if self.state != State.ENDING and self._end_after_live is None:
            self._show(0, self._face_for(self.state))

    async def _end_requested(self) -> None:
        """End the live without powering off (confirmed press, or ``press --long``)."""
        if self.state in LIVE_STATES:
            await self._begin_end(power_off=False)
        elif self.state == State.GOING_LIVE:
            if self._end_after_live is None:
                self._end_after_live = "end"
            log.info("the live will end as soon as Facebook confirms it")
            self._show(0, STATUS_FACES[State.ENDING])

    def _flash(self, key: int, face: KeyFace, back: KeyFace | Callable[[], KeyFace]) -> None:
        """Show ``face`` on ``key`` for FLASH_S, then ``back`` (a face or a function giving one)."""
        self._show(key, face)
        if self._loop is None:
            return
        old = self._flash_handles.pop(key, None)
        if old is not None:
            old.cancel()

        def restore() -> None:
            self._flash_handles.pop(key, None)
            self._show(key, back() if callable(back) else back)

        self._flash_handles[key] = self._loop.call_later(FLASH_S, restore)

    async def _go_live_pressed(self) -> None:
        if self.dry_run:
            log.info("dry-run: would go live")
            return
        self._go_live_pressed_at = time.time()
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
        elif self.state == State.GOING_LIVE:
            # Never cancel the go-live mid-click: a live that starts must be ended
            # properly (outro, report), then the PC powers off.
            self._end_after_live = "power"
            log.info("power pressed while going live: ending the live once it exists, then powering off")
            self._show(0, STATUS_FACES[State.ENDING])
        else:
            await self._start_flow(self._shutdown_flow, enter=State.ENDING)

    async def _begin_end(self, *, power_off: bool, outro: bool | None = None) -> None:
        await self._cancel_bounded(self._reattach, REATTACH_CANCEL_WAIT_S)
        await self._cancel_bounded(self._fresh, REATTACH_CANCEL_WAIT_S)
        if self._ended is not asyncio.current_task():
            await self._cancel_bounded(self._ended, REATTACH_CANCEL_WAIT_S)
        self._fresh_next = None
        self._end_armed_at = None
        self._end_after_live = None
        await self._cancel(self._restart)
        self._power_after_end = False
        # Not while RECONNECTING (nothing would air it), nor when the stream already stopped.
        self._outro = self.state == State.LIVE if outro is None else outro
        self._end_mode = "power" if power_off else "end"
        await self._start_flow(
            lambda: self._end_flow(power_off=power_off), enter=State.ENDING
        )

    async def _on_obs(self, ev: ObsStreamState) -> None:
        retrying, self._obs_retrying = self._obs_retrying, ev.reconnecting
        if self._paused:
            # Only follow what OBS reports; nothing is restarted or ended (resume decides).
            if self.state == State.LIVE and ev.active and ev.reconnecting:
                self._transition(State.RECONNECTING)
            elif self.state == State.RECONNECTING and ev.active and not ev.reconnecting:
                self._transition(State.LIVE)
            return
        if not ev.active:
            self._stream_stopped_at = self.clock()
            if self.state == State.READY:
                await self._redo_setup("OBS's pre-roll stream stopped")
                return
            if self.state == State.GOING_LIVE:
                log.warning("OBS's stream stopped while going live; ending the live once it exists")
                if self._end_after_live is None:
                    self._end_after_live = "obs"
                return
        if self.state not in LIVE_STATES:
            return
        if ev.reconnecting:
            if self.state == State.LIVE:
                self._transition(State.RECONNECTING)
        elif ev.active:
            if self.state == State.RECONNECTING:
                await self._cancel(self._restart)
                self._transition(State.LIVE)
        elif self.state == State.LIVE and not retrying and self._restart_idle():
            # Stopped without OBS's own reconnect first: the host clicked Stop Streaming
            # (a dropped connection makes OBS report RECONNECTING before it gives up).
            log.warning("OBS's stream was stopped in OBS while live; ending the live")
            await self._begin_end(power_off=False, outro=False)
        else:
            self._obs_gave_up()

    def _restart_idle(self) -> bool:
        return self._restart is None or self._restart.done()

    async def _on_tick(self) -> None:
        self._write_status()
        if self._paused:
            return  # paused: no checks that could change anything
        if (self.state == State.ERROR and self._flow_idle() and self._error_at is not None
                and self.clock() - self._error_at >= ERROR_RETRY_S):
            log.info("retrying setup %.0fs after the error", self.clock() - self._error_at)
            self._error_at = None
            await self._retry()
            return
        if self.state == State.READY and self._flow_idle():
            try:
                streaming = await self._bounded("stream_active", self.obs.stream_active())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("stream_active poll failed: %r", exc)
                streaming = True  # unknown: leave it to the Facebook check
            if not streaming:
                await self._redo_setup("OBS is not streaming the pre-roll")
                return
            try:
                lost = await self._bounded("preview_lost", self.fb.preview_lost())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("preview_lost failed: %r", exc)
                return
            if lost:
                await self._redo_setup("facebook preview lost")
        elif self.state in LIVE_STATES:
            self._maybe_reattach_capture()
            self._maybe_check_ended()
            if self.state == State.LIVE:
                self._maybe_check_fresh()
                await self._poll_obs_while_live()

    async def _redo_setup(self, why: str) -> None:
        """READY no longer holds (the key must not stay green): run setup again."""
        # Re-check: the world may have moved while we awaited.
        if self.state == State.READY and self._flow_idle() and not self._paused:
            log.warning("%s; re-running setup", why)
            await self._start_flow(lambda: self._setup_flow("SETUP"), enter=State.SETUP)

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

    def _maybe_check_ended(self) -> None:
        check = getattr(self.capture, "live_ended", None)
        live_id = self.live_id
        if check is None or live_id is None or not self._flow_idle():
            return
        if self.state != State.RECONNECTING or self._recon_at is None:
            return
        if self._ended is not None and not self._ended.done():
            return
        now = self.clock()
        if now - self._recon_at < ENDED_AFTER_S:
            return
        if self._ended_checked_at is not None and now - self._ended_checked_at < ENDED_EVERY_S:
            return
        self._ended_checked_at = now
        self._ended = asyncio.get_running_loop().create_task(self._check_ended(check, live_id))

    async def _check_ended(self, check: Callable[[str], Awaitable[bool]], live_id: str) -> None:
        try:
            ended = await asyncio.wait_for(check(live_id), FRESH_CHECK_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("capture.live_ended failed: %r", exc)
            return
        if not ended:
            log.info("still reconnecting; Facebook has not ended live %s", live_id)
            return
        if (self.state in LIVE_STATES and self.live_id == live_id and not self._paused
                and self._flow_idle()):
            log.warning("live %s was ended on Facebook; ending it here", live_id)
            await self._begin_end(power_off=False, outro=False)

    def _maybe_check_fresh(self) -> None:
        """Every FRESH_CHECK_S while LIVE, run ``capture.check_fresh`` in its own task.

        Only for capture objects that provide it (the browser adapter); at most
        one at a time, so a Tick is never held up by it.
        """
        check = getattr(self.capture, "check_fresh", None)
        live_id = self.live_id
        if check is None or live_id is None or not self._flow_idle():
            return
        if self._fresh is not None and not self._fresh.done():
            return
        if self._reattach is not None and not self._reattach.done():
            return  # it may be opening the column right now
        now = self.clock()
        if self._fresh_next is None:
            self._fresh_next = now + FRESH_CHECK_S
            return
        if now < self._fresh_next:
            return
        self._fresh_next = now + FRESH_CHECK_S
        self._fresh = asyncio.get_running_loop().create_task(self._check_fresh(check, live_id))

    @staticmethod
    async def _check_fresh(check: Callable[[str], Awaitable[object]], live_id: str) -> None:
        try:
            await asyncio.wait_for(check(live_id), FRESH_CHECK_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            log.warning("capture.check_fresh timed out after %.0fs (continuing)", FRESH_CHECK_TIMEOUT_S)
        except Exception:
            log.exception("capture.check_fresh failed (continuing)")

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
        if self._paused:
            return KeyFace("PAUSED\n" + _PAUSED_LABELS[state], _PAUSED_BG)
        if state == State.ERROR and self._error_step is not None:
            base = STATUS_FACES[State.ERROR]
            return dataclasses.replace(base, label="ERR\n" + self._error_step[:6].upper())
        return STATUS_FACES[state]

    def _transition(self, state: State) -> None:
        """Enter ``state``: persist, write status, update key 0. Never awaits."""
        if state == State.RECONNECTING and self.state != State.RECONNECTING:
            self._recon_at, self._ended_checked_at = self.clock(), None
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
        if state in LIVE_STATES or state == State.GOING_LIVE:
            armed = self._end_armed_at
            if self._end_after_live is not None:
                self._show(0, STATUS_FACES[State.ENDING])
            elif armed is not None and self.clock() - armed <= END_CONFIRM_S:
                self._show(0, _FACE_PRESS_AGAIN)
        else:
            self._end_armed_at = None
        for f in self.features:
            hook = getattr(f, "state_changed", None)
            if f.enabled and hook is not None:
                try:
                    hook(state)  # synchronous, like this method
                except Exception:
                    log.exception("feature %s: state_changed failed (ignored)", f.name)

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
                features=self.cfg.features(),
                paused=self._paused,
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
        self._error_at = self.clock()
        log.error("ERROR %s", self.last_error)
        self._transition(State.ERROR)

    async def _fail(self, step: str, detail: str) -> None:
        pending, self._end_after_live = self._end_after_live, None
        directory = self.cfg.paths.failures_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{step}"
        try:
            await self.fb.save_failure(step, directory)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("save_failure(%s) failed", step)
        self._enter_error(step, detail)
        if pending == "power":  # power was pressed while going live, and no live started
            self._spawn_flow_soon(self._shutdown_flow, State.ENDING)

    def _spawn_end_soon(self, *, power_off: bool, outro: bool) -> None:
        async def start() -> None:
            await self._begin_end(power_off=power_off, outro=outro)

        self._spawn_feature_task(start())

    def _spawn_flow_soon(self, factory: Callable[[], Awaitable[None]], enter: State) -> None:
        """Start a flow from inside the running one (which is about to return)."""
        async def start() -> None:
            await self._start_flow(factory, enter=enter)

        self._spawn_feature_task(start())

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

            await self._refresh_ssn()
            await self._login_phase()

        if not await self._setup_phase():
            return  # a live is running: leave it (and its state) alone
        self._transition(State.READY)

    def _on_ssn_stalled(self) -> None:
        """SSN's comment relay is down: have it reopen its background tab, in any state.

        Only SSN's own tab is touched (not the stream, the comments popout or the
        DOM capture), so this is safe while LIVE.
        """
        if self._ssn_heal is not None and not self._ssn_heal.done():
            return
        now = self.clock()
        if self._ssn_healed_at is not None and now - self._ssn_healed_at < SSN_HEAL_MIN_INTERVAL_S:
            return
        self._ssn_healed_at = now
        log.info("SSN reported its background page stopped; having it reopened")
        self._ssn_heal = asyncio.get_running_loop().create_task(self._refresh_ssn())

    async def _refresh_ssn(self) -> None:
        """Best effort: SSN's background tab can get stuck; closing it makes SSN reopen it."""
        try:
            await asyncio.wait_for(self.fb.refresh_ssn(), SSN_REFRESH_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("SSN background refresh failed: %r", exc)

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
                # Facebook only shows a stream that connects after this setup's live
                # draft exists, so a running pre-roll is always replaced. It is stopped
                # before the setup steps, which then count toward the reconnect wait.
                if await self.obs.stream_active():
                    # SETUP: no live exists (the live only starts with go-live),
                    # so replacing the pre-roll stream is safe. Local guard: never
                    # stop the stream if we somehow are in a live state.
                    if self._setup_blocked_by_live():
                        return False  # never touch a live's stream (nor ERROR it)
                    # OBS refuses SetStreamServiceSettings while streaming.
                    await self.obs.stop_stream()
                    await self._wait_stream_stopped()
                    self._stream_stopped_at = self.clock()
                target = await self.fb.setup_live()
                await self.obs.set_stream_target(target)
                wait = self._reconnect_wait_left()
                if wait > 0:
                    log.info("waiting %.0fs before reconnecting: Facebook refuses the key "
                             "right after a stream stopped", wait)
                    await self._sleep(wait)
                await self._feature_hook("before_preroll")
                await self.obs.start_stream()
                await self._wait_preview_keeping_pre_roll(t.preview_s)
                return True
            except StepFailed as exc:
                log.warning("setup attempt %d/%d failed: %s", attempt, attempts, exc)
                if attempt >= attempts:
                    raise
        return True  # unreachable: the loop returns or raises

    def _reconnect_wait_left(self) -> float:
        if self._stream_stopped_at is None:
            return 0.0
        return max(0.0, PREROLL_RECONNECT_WAIT_S - (self.clock() - self._stream_stopped_at))

    async def _wait_preview_keeping_pre_roll(self, timeout_s: float) -> None:
        preview = asyncio.ensure_future(self.fb.wait_preview_ready(timeout_s))
        try:
            while True:
                done, _ = await asyncio.wait({preview}, timeout=PREROLL_CHECK_S)
                if done:
                    return preview.result()
                if self._setup_blocked_by_live():
                    return await preview
                if self._reconnect_wait_left() > 0:
                    continue  # a refused connect just stopped it; retrying now is refused too
                if not await self.obs.stream_active():
                    log.warning("OBS pre-roll stream stopped before Facebook saw it; restarting it")
                    await self.obs.start_stream()
        finally:
            if not preview.done():
                preview.cancel()
                await asyncio.gather(preview, return_exceptions=True)

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
        if not active and self._paused:
            log.warning("resume: saved live %s, OBS is not streaming; paused, so nothing changes", live_id)
            return
        if not active:
            log.warning("resume: saved live %s but OBS is not streaming; starting setup", live_id)
            await self._cancel(self._restart)
            self.live_id = None
            self.stream_dir = None
            self._spawn_feature_task(self._feature_hook("not_live"))
            await self._setup_flow("STARTING")
            return
        self._mkdir(self.stream_dir)
        await self._feature_hook("live_resumed", live_id, self.stream_dir)
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
        self._obs_retrying = False
        self._transition(State.LIVE)
        # Best effort: nothing below may turn into ERROR.
        await self._feature_hook("live_confirmed", live_id, stream_dir, self._go_live_pressed_at)
        pending = self._end_after_live
        if pending is not None:
            # Ended while going live (two presses, power, or OBS stopped): end it now.
            log.info("ending the live that was just confirmed (%s)", pending)
            self._spawn_end_soon(power_off=pending == "power", outro=pending != "obs")
            return
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

    async def _bounded_step(
        self, name: str, aw: Awaitable[object], *, reserve: float | None = None,
        cap: float | None = None, floor: float = 0.0,
    ) -> None:
        """Best-effort end-flow step, at most END_STEP_TIMEOUT_S (read at call time) and
        at most what is left of the end-flow budget after ``reserve`` (default
        END_TAIL_RESERVE_S), but never less than ``floor``; skipped when nothing is left."""
        cap = END_STEP_TIMEOUT_S if cap is None else cap
        timeout = max(self._end_budget(cap, reserve), min(floor, cap))
        if timeout <= 0:
            if asyncio.iscoroutine(aw):
                aw.close()
            log.warning("%s skipped: the end flow's time budget is used up", name)
            return
        await self._best_effort(name, aw, timeout)

    def _end_budget(self, cap: float, reserve: float | None = None) -> float:
        if self._end_deadline is None:
            return cap
        reserve = END_TAIL_RESERVE_S if reserve is None else reserve
        return min(cap, self._end_deadline - self.clock() - reserve)

    async def _end_hook(self, hook: str, *args: object, cap: float = END_HOOK_TIMEOUT_S) -> None:
        """A feature hook in the end flow, bounded by what is left of the budget."""
        timeout = self._end_budget(cap)
        if timeout <= 0:
            log.warning("feature hook %s skipped: the end flow's time budget is used up", hook)
            return
        await self._feature_hook(hook, *args, timeout=timeout)

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
        self._end_deadline = self.clock() + END_FLOW_BUDGET_S
        stream_dir = self.stream_dir
        self._reset_reattach_backoff()
        if self._outro:
            self._outro = False
            await self._end_hook("before_stop_stream", cap=END_OUTRO_CAP_S)
        # Stopping the stream is never cut short by the budget: it ends the live.
        await self._best_effort("stop_stream", self.obs.stop_stream(), END_STEP_TIMEOUT_S)
        await self._wait_stream_stopped()
        ended_at = time.time()

        await self._sleep(self.cfg.timeouts.fb_end_wait_s)
        if stream_dir is not None:
            await self._end_hook("stream_stopped", stream_dir, ended_at)
        await self._bounded_step("capture.stop", self.capture.stop())
        self._write_report()
        if stream_dir is not None:
            # stream_closed writes the folder's last files, stream_done marks it DONE
            # and copies it off (upload: at most final_timeout_s).
            await self._end_hook("stream_closed", stream_dir)
            await self._end_hook(
                "stream_done", stream_dir,
                cap=max(END_HOOK_TIMEOUT_S, self.cfg.upload.final_timeout_s + 2),
            )

        # Branch point (synchronous with the flag check, so a PowerPress either
        # sets the flag before it or is handled as a plain shutdown after it).
        if power_off or self._power_after_end:
            self._end_mode = "power"
            await self._power_steps()
            return
        self._end_mode = None
        self._end_deadline = None
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
        self._end_deadline = self.clock() + END_FLOW_BUDGET_S
        await self._power_steps()

    async def _power_steps(self) -> None:
        await self._bounded_step(
            "close_browser", self.fb.close_browser(),
            reserve=END_TAIL_RESERVE_S / 2, floor=END_TAIL_MIN_S,
        )
        # Both units restart on any exit (Restart=always): stop them so neither comes back.
        await self._bounded_step(
            "stop chromium.service", self.system.stop_user_unit("chromium.service"),
            reserve=0.0, floor=END_TAIL_MIN_S,
        )
        await self._bounded_step(
            "stop obs.service", self.system.stop_user_unit("obs.service"),
            reserve=0.0, floor=END_TAIL_MIN_S,
        )
        await self._best_effort("poweroff", self.system.poweroff())

    # ------------------------------------------------------------ deck cameras

    def _camera_face(self, cam: str) -> KeyFace:
        bg = _CAMERA_ACTIVE_BG if cam == self._active_camera else _CAMERA_IDLE_BG
        return KeyFace(_CAMERA_FACES[cam], bg)

    def _show_camera_faces(self) -> None:
        for key, cam in _CAMERA_KEYS.items():
            self._show(key, self._camera_face(cam))

    def _show_autofocus(self) -> None:
        cam = self._active_camera
        if cam is not None and self._autofocus.get(cam, False):
            self._show(1, _FACE_AF_ON)
        elif cam is not None and cam in self._macro:
            self._show(1, _FACE_MACRO)
        else:
            self._show(1, _FACE_AF_OFF)

    def _scene_of(self, cam: str) -> str:
        scene = self.cfg.cameras[cam].scene
        return self._scene_alias.get(scene, scene)

    async def _refresh_scene_alias(self) -> bool:
        """Match the configured camera scenes to OBS's scene names; True if any changed."""
        request = getattr(self.obs, "request", None)
        if request is None:
            return False
        try:
            names = await self._bounded("GetSceneList", obs_scene_names(self.obs))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("cannot list OBS's scenes: %r", exc)
            return False
        alias: dict[str, str] = {}
        for cam, c in self.cfg.cameras.items():
            actual = resolve_scene(c.scene, names)
            if actual is None:
                log.warning("camera %s: OBS has no scene %r (scenes: %s)",
                            cam, c.scene, ", ".join(names))
            elif actual != c.scene:
                log.warning("camera %s: using OBS's scene %r for the configured %r",
                            cam, actual, c.scene)
                alias[c.scene] = actual
        changed = alias != self._scene_alias
        self._scene_alias = alias
        return changed

    def _on_program_scene(self, name: str) -> None:
        cam = next((c for c in self.cfg.cameras if self._scene_of(c) == name), None)
        if cam is None and self.state not in LIVE_STATES:
            return  # e.g. the holding card: the highlight shows the camera picked for after it
        if cam != self._active_camera:
            self._active_camera = cam
            self._show_camera_faces()
            self._show_autofocus()

    async def _camera_press(self, cam: str) -> None:
        cfg = self.cfg.cameras.get(cam)
        if cfg is None:
            log.warning("no camera %r configured", cam)
            return
        key = next(k for k, c in _CAMERA_KEYS.items() if c == cam)
        for attempt in (1, 2):
            scene = self._scene_of(cam)
            try:
                claimed = self.state not in LIVE_STATES and await self._feature_claims(
                    "select_camera", scene
                )
                if not claimed:
                    await self._bounded("set_scene", self.obs.set_scene(scene))
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                missing = isinstance(exc, ObsRequestFailed) and exc.code == CODE_RESOURCE_NOT_FOUND
                if attempt == 1 and missing and await self._refresh_scene_alias():
                    continue  # OBS's scene was renamed: try its current name
                log.warning("camera %s: switching to scene %r failed: %r", cam, scene, exc)
                self._flash(key, _FACE_NO_SCENE, lambda: self._camera_face(cam))
                return
        self._active_camera = cam
        try:
            await self._bounded(
                "set_autofocus", self.camera.set_autofocus(cfg.device, True)
            )
            self._autofocus[cam] = True
            self._macro.discard(cam)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("autofocus on %s failed", cfg.device)
        self._show_camera_faces()
        self._show_autofocus()

    async def _autofocus_press(self) -> None:
        """Toggle the active camera between continuous autofocus and macro (autofocus
        off, focus at its closest), for holding small items up to the lens."""
        cam = self._active_camera
        if cam is None:
            cam = await self._camera_from_scene()
        cfg = self.cfg.cameras.get(cam)
        if cfg is None:
            log.warning("no camera %r configured", cam)
            return
        macro = self._autofocus.get(cam, False)
        try:
            if macro:
                await self._bounded("set_macro", self.camera.set_macro(cfg.device))
            else:
                await self._bounded(
                    "set_autofocus", self.camera.set_autofocus(cfg.device, True)
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s on %s failed", "macro" if macro else "autofocus", cfg.device)
            return
        self._active_camera = cam
        self._autofocus[cam] = not macro
        if macro:
            self._macro.add(cam)
        else:
            self._macro.discard(cam)
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
        cam = next((n for n in self.cfg.cameras if self._scene_of(n) == scene), None)
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
            if self._scene_of(name) == scene:
                return name
        return "main"
