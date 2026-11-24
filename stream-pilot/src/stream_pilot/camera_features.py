"""``[wb_freeze]`` and ``[audio_filters]``: camera white balance and microphone filters.

**White-balance freeze.** The close-up camera's auto white balance drifts as
items of different colors fill its picture. While its scene is on air (OBS's
program scene, from OBS's scene-change events, so changes made in OBS count as
well as deck presses) its white balance is frozen; when it leaves, auto is
turned back on. Measured on the C920 (2026-11-24 hardware notes): turning auto
off jumps to the camera's *stored* manual value, the value can only be written
once auto is off, and the temperature reported under auto does not match the
picture auto produces (about 300 K off). So a freeze writes the camera's last
reported auto temperature right after turning auto off: the picture shifts
visibly and shows the stored value for a moment. That is why the switch ships
off. ``rebalance()`` (``stream-pilot rebalance``) turns auto on for
``rebalance_s`` and freezes again. ``freeze_exposure`` also freezes exposure.

**Mic filters.** OBS's noise suppression (RNNoise) and a limiter (-1 dB) on the
microphone source, created over obs-websocket if missing, enabled or disabled
with the switch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .camera_ctl import AUTO_EXPOSURE_APERTURE_PRIORITY, AUTO_EXPOSURE_MANUAL, V4l2Controls
from .config import Config
from .features import Feature
from .obs_items import Filter, apply_items

log = logging.getLogger(__name__)

SCENE_EVENT = "CurrentProgramSceneChanged"
APPLY_TIMEOUT_S = 10.0


class WbFreeze(Feature):
    name = "wb_freeze"

    def __init__(
        self,
        cfg: Config,
        *,
        obs: Any,
        controls: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        super().__init__(cfg.wb_freeze.enabled)
        self.cfg = cfg
        self.obs = obs
        wb = cfg.wb_freeze
        cam = cfg.cameras.get(wb.camera)
        if cam is None:
            log.warning("wb_freeze: no camera %r configured", wb.camera)
        self.scene = cam.scene if cam else None
        self.ctl = controls or V4l2Controls(cam.device if cam else "")
        self.freeze_exposure = wb.freeze_exposure
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._frozen = False
        self._tasks: set[asyncio.Task] = set()
        if self.enabled and self.scene and obs is not None:
            obs.on_event(SCENE_EVENT, self._on_scene_changed)

    # -- switch ------------------------------------------------------------------

    async def restore(self) -> None:
        """Off (and at startup): white balance back to auto, exposure too if it is manual."""
        async with self._lock:
            await self._auto()

    async def started(self) -> None:
        await self.restore()

    async def obs_connected(self) -> None:
        data = await self.obs.request("GetCurrentProgramScene") or {}
        await self._apply(data.get("currentProgramSceneName") or data.get("sceneName"))

    # -- scene changes -----------------------------------------------------------

    def _on_scene_changed(self, data: dict | None) -> None:
        scene = (data or {}).get("sceneName")
        task = asyncio.get_running_loop().create_task(
            asyncio.wait_for(self._apply(scene), APPLY_TIMEOUT_S)
        )
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("wb_freeze: applying a scene change failed: %r", task.exception())

    async def _apply(self, program: str | None) -> None:
        async with self._lock:
            on_air = program == self.scene
            if on_air and not self._frozen:
                await self._freeze()
            elif not on_air and self._frozen:
                await self._unfreeze()

    # -- actions -----------------------------------------------------------------

    async def rebalance(self, settle_s: float | None = None) -> None:
        """Auto white balance on for ``rebalance_s``, then frozen again."""
        async with self._lock:
            names = await self.ctl.names()
            if names.wb_auto is None:
                log.warning("wb_freeze: %s has no auto white balance control", self.ctl.device)
                return
            values = {names.wb_auto: 1}
            if self.freeze_exposure and names.exposure_auto:
                values[names.exposure_auto] = AUTO_EXPOSURE_APERTURE_PRIORITY
            await self.ctl.set(**values)
            await self._sleep(self.cfg.wb_freeze.rebalance_s if settle_s is None else settle_s)
            await self._freeze()

    async def _freeze(self) -> None:
        names = await self.ctl.names()
        if names.wb_auto is None or names.wb_temp is None:
            log.warning("wb_freeze: %s has no white balance controls; not frozen", self.ctl.device)
            return
        temp = await self.ctl.get(names.wb_temp)
        await self.ctl.set(**{names.wb_auto: 0})
        # The camera now shows its stored value; only now can the temperature be written.
        if temp is not None:
            await self.ctl.set(**{names.wb_temp: temp})
        if self.freeze_exposure and names.exposure_auto and names.exposure_abs:
            exposure = await self.ctl.get(names.exposure_abs)
            await self.ctl.set(**{names.exposure_auto: AUTO_EXPOSURE_MANUAL})
            if exposure is not None:
                await self.ctl.set(**{names.exposure_abs: exposure})
        self._frozen = True
        log.info("wb_freeze: %s frozen at %s K", self.ctl.device, temp)

    async def _unfreeze(self) -> None:
        await self._auto()
        log.info("wb_freeze: %s back on auto", self.ctl.device)

    async def _auto(self) -> None:
        names = await self.ctl.names()
        if names.wb_auto is not None:
            await self.ctl.set(**{names.wb_auto: 1})
        if names.exposure_auto is not None and await self.ctl.get(names.exposure_auto) == AUTO_EXPOSURE_MANUAL:
            await self.ctl.set(**{names.exposure_auto: AUTO_EXPOSURE_APERTURE_PRIORITY})
        self._frozen = False


async def find_mic_input(obs: Any, mic_device: str) -> str | None:
    """The microphone's OBS input: the PulseAudio input with ``mic_device``, else
    "Microphone", else the first PulseAudio input."""
    data = await obs.request("GetInputList", {"inputKind": "pulse_input_capture"}) or {}
    names = [i.get("inputName") for i in data.get("inputs", [])
             if i.get("inputKind", "pulse_input_capture") == "pulse_input_capture"]
    for name in names:
        settings = await obs.request("GetInputSettings", {"inputName": name}) or {}
        if settings.get("inputSettings", {}).get("device_id") == mic_device:
            return name
    if "Microphone" in names:
        return "Microphone"
    return names[0] if names else None


class AudioFilters(Feature):
    name = "audio_filters"

    def __init__(self, cfg: Config, *, obs: Any):
        super().__init__(cfg.audio_filters.enabled)
        self.cfg = cfg
        self.obs = obs

    async def items(self) -> list[Filter]:
        mic = await find_mic_input(self.obs, self.cfg.audio.mic_device)
        if mic is None:
            log.warning("audio_filters: no microphone input in OBS")
            return []
        af = self.cfg.audio_filters
        return [
            Filter(mic, "Noise Suppression (stream-pilot)", "noise_suppress_filter_v2",
                   {"method": "rnnoise"}),
            Filter(mic, "Limiter (stream-pilot)", "limiter_filter",
                   {"threshold": float(af.limiter_db), "release": 60}),
        ]

    async def obs_connected(self) -> None:
        await apply_items(self.obs, await self.items(), True, owner="audio_filters")

    async def restore(self) -> None:
        await apply_items(self.obs, await self.items(), False, owner="audio_filters")
