"""``[deck_warnings]``: warnings on the spare Stream Deck key (they never block going live).

In READY, LIVE and RECONNECTING the spare key shows the first warning that holds:

1. ``MIC?``: no sound above ``mic_silent_db`` (peak) for ``mic_silent_s``. Levels
   come from OBS's InputVolumeMeters event (every 50 ms; only every
   ``METER_EVERY_S`` is looked at). Before Go Live a holding scene may keep the
   microphone off the air (OBS then reports no level for it): that is not a
   warning; while live it is.
2. ``CAM?``: a camera in ``cameras`` is missing (its device, or the device its OBS
   source uses), or it is on air and OBS says its source is not active.
3. ``NET``: in a sliding ``net_window_s``, most 2 s samples of GetStreamStatus
   show dropped frames above ``net_skipped_pct`` % or congestion above
   ``net_congestion``. A single short burst (one 10 s congestion burst dropped
   75 frames on the stream PC) covers only half the window and does not count.
4. ``MEM``: the memory guard's warning (memory.py), which otherwise owns the key.

In other states only ``MEM`` is shown, as without this feature. Off: the memory
guard drives the key as today and nothing here runs.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import deque
from typing import Any, Awaitable, Callable

from .camera_features import find_mic_input
from .config import Config
from .features import Feature
from .memory import BLANK_FACE, MEM_FACE, SPARE_KEY
from .states import KeyFace, State

log = logging.getLogger(__name__)

STEP_S = 2.0
CAMERA_EVERY_S = 10.0
METER_EVERY_S = 0.5
# Share of the window's samples that must be bad for NET (and how many samples it needs).
NET_BAD_SHARE = 0.8
_REPORTED_RECENTLY_S = 5.0
_SHOWN_IN = frozenset({State.READY, State.LIVE, State.RECONNECTING})
FACES = {
    "MIC?": KeyFace("MIC?", "#e67e00", blink=True),
    "CAM?": KeyFace("CAM?", "#e67e00", blink=True),
    "NET": KeyFace("NET", "#e67e00", blink=True),
    "MEM": MEM_FACE,
}


def peak_db(levels: Any) -> float:
    """Highest channel peak of an inputLevelsMul list ([[magnitude, peak, input peak], ...])."""
    peak = 0.0
    for channel in levels or []:
        if isinstance(channel, (list, tuple)) and len(channel) > 1:
            try:
                peak = max(peak, float(channel[1]))
            except (TypeError, ValueError):
                continue
    return 20 * math.log10(peak) if peak > 0 else -math.inf


class NetWindow:
    """Bad/good 2 s samples of GetStreamStatus over a sliding window."""

    def __init__(self, window_s: float, skipped_pct: float, congestion: float):
        self.window_s = window_s
        self.skipped_pct = skipped_pct
        self.congestion = congestion
        self._last: tuple[int, int] | None = None
        self._samples: deque[tuple[float, bool]] = deque()

    def reset(self) -> None:
        self._last = None
        self._samples.clear()

    def add(self, now: float, status: dict) -> None:
        if not status.get("outputActive"):
            self.reset()
            return
        skipped, total = status.get("outputSkippedFrames", 0), status.get("outputTotalFrames", 0)
        if self._last is not None and total >= self._last[1]:
            d_total = total - self._last[1]
            d_skipped = max(0, skipped - self._last[0])
            bad = (d_total > 0 and 100.0 * d_skipped / d_total > self.skipped_pct) or (
                float(status.get("outputCongestion") or 0.0) > self.congestion
            )
            self._samples.append((now, bad))
        self._last = (skipped, total)
        while self._samples and now - self._samples[0][0] > self.window_s:
            self._samples.popleft()

    def warn(self) -> bool:
        needed = max(1, int(self.window_s / STEP_S * NET_BAD_SHARE))
        bad = sum(1 for _, b in self._samples if b)
        return len(self._samples) >= needed and bad >= needed


class DeckWarnings(Feature):
    name = "deck_warnings"

    def __init__(
        self,
        cfg: Config,
        *,
        obs: Any,
        deck: Any,
        exists: Callable[[str], bool] = os.path.exists,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        super().__init__(cfg.deck_warnings.enabled)
        self.cfg = cfg
        self.obs = obs
        self.deck = deck
        self._exists = exists
        self._clock = clock
        self._sleep = sleep
        dw = cfg.deck_warnings
        self.net = NetWindow(dw.net_window_s, dw.net_skipped_pct, dw.net_congestion)
        self.state = State.STARTING
        self.mem = False
        self.cam = False
        self._mic: str | None = None
        self._sound_at = clock()
        self._reported_at: float | None = None
        self._meter_at = -math.inf
        self._camera_at = -math.inf
        self._shown: str | None = None
        self._rendered = False
        if self.enabled:
            obs.on_event("InputVolumeMeters", self.on_meters)

    # -- inputs ------------------------------------------------------------------

    def state_changed(self, state: State) -> None:
        if state in (State.READY, State.LIVE) and state != self.state:
            self._sound_at = max(self._sound_at, self._clock())  # a fresh MIC? countdown
        if state not in _SHOWN_IN:
            self.net.reset()
        self.state = state
        self.render()

    def show(self, key: int, face: KeyFace) -> None:
        """The memory guard's output (it is handed this object instead of the deck)."""
        if key == SPARE_KEY:
            self.mem = face == MEM_FACE
            self.render()
        else:
            self.deck.show(key, face)

    def on_meters(self, data: dict | None) -> None:
        now = self._clock()
        if now - self._meter_at < METER_EVERY_S or self._mic is None:
            return
        self._meter_at = now
        for item in (data or {}).get("inputs", []):
            if item.get("inputName") != self._mic:
                continue
            self._reported_at = now
            if peak_db(item.get("inputLevelsMul")) > self.cfg.deck_warnings.mic_silent_db:
                self._sound_at = now
            return

    async def obs_connected(self) -> None:
        self._mic = await find_mic_input(self.obs, self.cfg.audio.mic_device)

    # -- evaluation --------------------------------------------------------------

    def mic_silent(self) -> bool:
        now = self._clock()
        if now - self._sound_at <= self.cfg.deck_warnings.mic_silent_s:
            return False
        if self.state == State.LIVE:
            return True
        # Before Go Live the microphone may be off the air on purpose (holding scene).
        return self._reported_at is not None and now - self._reported_at < _REPORTED_RECENTLY_S

    async def check_cameras(self) -> bool:
        program = None
        try:
            data = await self.obs.request("GetCurrentProgramScene") or {}
            program = data.get("currentProgramSceneName") or data.get("sceneName")
        except Exception as exc:
            log.debug("deck_warnings: GetCurrentProgramScene failed: %r", exc)
        for name in self.cfg.deck_warnings.cameras:
            cam = self.cfg.cameras.get(name)
            if cam is None:
                continue
            if not self._exists(cam.device):
                return True
            try:
                items = (await self.obs.request("GetSceneItemList", {"sceneName": cam.scene})
                         or {}).get("sceneItems", [])
            except Exception as exc:
                log.debug("deck_warnings: GetSceneItemList(%s) failed: %r", cam.scene, exc)
                continue
            for item in items:
                if item.get("inputKind") != "v4l2_input":
                    continue
                source = item.get("sourceName")
                settings = (await self.obs.request("GetInputSettings", {"inputName": source})
                            or {}).get("inputSettings", {})
                device = settings.get("device_id")
                if device and not self._exists(device):
                    return True
                if program == cam.scene:
                    active = await self.obs.request("GetSourceActive", {"sourceName": source}) or {}
                    if active.get("videoActive") is False:
                        return True
        return False

    def warning(self) -> str | None:
        if self.state in _SHOWN_IN:
            if self.mic_silent():
                return "MIC?"
            if self.cam:
                return "CAM?"
            if self.net.warn():
                return "NET"
        return "MEM" if self.mem else None

    def render(self) -> None:
        w = self.warning()
        if self._rendered and w == self._shown:
            return
        try:
            self.deck.show(SPARE_KEY, FACES[w] if w else BLANK_FACE)
        except Exception:
            log.exception("deck_warnings: cannot update the spare key")
            return
        if w != self._shown:
            log.info("deck warning: %s", w or "none")
        self._shown, self._rendered = w, True

    async def step(self) -> None:
        now = self._clock()
        if self.state in _SHOWN_IN:
            try:
                self.net.add(now, await self.obs.request("GetStreamStatus") or {})
            except Exception as exc:
                log.debug("deck_warnings: GetStreamStatus failed: %r", exc)
            if now - self._camera_at >= CAMERA_EVERY_S:
                self._camera_at = now
                try:
                    self.cam = await self.check_cameras()
                except Exception as exc:
                    log.debug("deck_warnings: camera check failed: %r", exc)
        self.render()

    async def run(self) -> None:
        """The background loop (app.py starts it only while the switch is on)."""
        while True:
            try:
                await asyncio.wait_for(self.step(), STEP_S * 2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("deck_warnings: step failed: %r", exc)
            await self._sleep(STEP_S)
