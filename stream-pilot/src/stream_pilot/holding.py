"""``[holding]``: a holding scene on air before Go Live, so the pre-roll never airs the room.

The microphone is a source inside each camera scene (not a global audio
device; see the 2026-11-24 hardware notes), so a scene without it is silent:
audio follows the scene and there is no mute state to forget.

* ``mode = "studio"``: before the pre-roll starts, OBS's Studio Mode is turned
  on with the holding scene ("Holding": a plain background and the text) on
  air and the main camera in the preview. Camera keys change the preview. When
  Facebook confirms the live, the previewed camera scene goes on air and Studio
  Mode is turned off.
* ``mode = "banner"``: before the pre-roll starts, "Starting Soon" goes on air:
  the main camera's picture with a text banner, no microphone. Camera keys only
  choose the scene that goes on air when Facebook confirms the live.

While live, a holding scene is never put on air; on resume, a camera scene is.
The scenes are created over obs-websocket if missing (obs_items.py), never
changed afterwards, so the host can restyle them in OBS.

Off: no scene change before Go Live (today's behavior). The restore hides the
holding scenes' items and, if a holding scene is on air (the switch was on
before), puts the main camera scene on air and turns Studio Mode off.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .config import Config
from .features import Feature
from .obs_items import Item, Scene, SceneItem, apply_items

log = logging.getLogger(__name__)

STUDIO_SCENE = "Holding"
BANNER_SCENE = "Starting Soon"
HOLDING_SCENES = (STUDIO_SCENE, BANNER_SCENE)
MODES = ("studio", "banner")
_CANVAS_W, _CANVAS_H = 1080, 1920
_FONT = {"face": "DejaVu Sans", "size": 96, "style": "Bold", "flags": 1}
_WHITE = 0xFFFFFFFF
_ALIGN_CENTER = 0
_ALIGN_TOP_LEFT = 5


def studio_items(text: str) -> list[Item]:
    return [
        Scene(STUDIO_SCENE),
        SceneItem(STUDIO_SCENE, "Holding Background", kind="color_source_v3",
                  settings={"color": 0xFF202020, "width": _CANVAS_W, "height": _CANVAS_H},
                  transform={"positionX": 0.0, "positionY": 0.0, "alignment": _ALIGN_TOP_LEFT}),
        SceneItem(STUDIO_SCENE, "Holding Text", kind="text_ft2_source_v2",
                  settings={"text": text, "font": dict(_FONT), "color1": _WHITE, "color2": _WHITE},
                  transform={"positionX": _CANVAS_W / 2, "positionY": _CANVAS_H / 2,
                             "alignment": _ALIGN_CENTER}),
    ]


def banner_items(text: str, camera_source: str | None, camera_transform: dict | None) -> list[Item]:
    items: list[Item] = [Scene(BANNER_SCENE)]
    if camera_source:
        items.append(SceneItem(BANNER_SCENE, camera_source, transform=camera_transform))
    items += [
        SceneItem(BANNER_SCENE, "Starting Soon Bar", kind="color_source_v3",
                  settings={"color": 0xCC000000, "width": _CANVAS_W, "height": 220},
                  transform={"positionX": 0.0, "positionY": _CANVAS_H - 420.0,
                             "alignment": _ALIGN_TOP_LEFT}),
        SceneItem(BANNER_SCENE, "Starting Soon Text", kind="text_ft2_source_v2",
                  settings={"text": text, "font": dict(_FONT), "color1": _WHITE, "color2": _WHITE},
                  transform={"positionX": _CANVAS_W / 2, "positionY": _CANVAS_H - 310.0,
                             "alignment": _ALIGN_CENTER}),
    ]
    return items


# SceneItemTransform fields that can be set (the rest are read-only results).
_SETTABLE = ("positionX", "positionY", "rotation", "scaleX", "scaleY", "alignment",
             "boundsType", "boundsAlignment", "boundsWidth", "boundsHeight",
             "cropLeft", "cropRight", "cropTop", "cropBottom")


class Holding(Feature):
    name = "holding"
    restore_needs_obs = True

    def __init__(self, cfg: Config, *, obs: Any,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        super().__init__(cfg.holding.enabled)
        self.cfg = cfg
        self.obs = obs
        self._sleep = sleep
        self.mode = cfg.holding.mode if cfg.holding.mode in MODES else "studio"
        if cfg.holding.mode not in MODES:
            log.warning("holding: unknown mode %r; using studio", cfg.holding.mode)
        self.main_scene = cfg.cameras["main"].scene if "main" in cfg.cameras else ""
        self._chosen = self.main_scene

    # -- OBS items -----------------------------------------------------------

    async def _items(self) -> list[Item]:
        if self.mode == "studio":
            return studio_items(self.cfg.holding.text)
        source, transform = await self._main_camera_source()
        return banner_items(self.cfg.holding.text, source, transform)

    async def _main_camera_source(self) -> tuple[str | None, dict | None]:
        """The main camera input in the main camera scene and its transform (no microphone)."""
        data = await self.obs.request("GetSceneItemList", {"sceneName": self.main_scene}) or {}
        for item in data.get("sceneItems", []):
            if item.get("inputKind") == "v4l2_input":
                name = item.get("sourceName")
                try:
                    t = await self.obs.request(
                        "GetSceneItemTransform",
                        {"sceneName": self.main_scene, "sceneItemId": item["sceneItemId"]},
                    ) or {}
                except Exception:
                    t = {}
                transform = {k: v for k, v in (t.get("sceneItemTransform") or {}).items()
                             if k in _SETTABLE}
                if transform.get("boundsType") == "OBS_BOUNDS_NONE":
                    for k in ("boundsType", "boundsAlignment", "boundsWidth", "boundsHeight"):
                        transform.pop(k, None)
                return name, transform or None
        log.warning("holding: no camera input in %r; the banner has no picture", self.main_scene)
        return None, None

    async def obs_connected(self) -> None:
        await apply_items(self.obs, await self._items(), True, owner="holding")

    async def restore(self) -> None:
        await apply_items(self.obs, studio_items(self.cfg.holding.text), False, owner="holding")
        source, _ = await self._main_camera_source()
        await apply_items(self.obs, banner_items(self.cfg.holding.text, source, None), False,
                          owner="holding")
        program = await self._program()
        if program in HOLDING_SCENES:
            log.info("holding: switched off; putting %r on air", self.main_scene)
            await self._camera_on_air(self.main_scene)

    # -- before Go Live --------------------------------------------------------

    async def before_preroll(self) -> None:
        await apply_items(self.obs, await self._items(), True, owner="holding")
        if self.mode == "studio":
            # In Studio Mode OBS swaps preview and program when a transition ends, so
            # the holding scene goes on air (and finishes its transition) first.
            if await self._studio_mode():
                await self.obs.request("SetStudioModeEnabled", {"studioModeEnabled": False})
            if await self._program() != STUDIO_SCENE:
                await self.obs.request("SetCurrentProgramScene", {"sceneName": STUDIO_SCENE})
                await self._sleep(await self._transition_s())
            await self.obs.request("SetStudioModeEnabled", {"studioModeEnabled": True})
            await self.obs.request("SetCurrentPreviewScene", {"sceneName": self._chosen})
        else:
            if await self._studio_mode():  # e.g. left on by studio mode before a mode change
                await self.obs.request("SetStudioModeEnabled", {"studioModeEnabled": False})
            await self.obs.request("SetCurrentProgramScene", {"sceneName": BANNER_SCENE})
        log.info("holding: %r on air before Go Live (%s mode)",
                 STUDIO_SCENE if self.mode == "studio" else BANNER_SCENE, self.mode)

    async def select_camera(self, scene: str) -> bool:
        """A camera key before Go Live: True if handled (the on-air scene must not change)."""
        if await self._program() not in HOLDING_SCENES:
            return False  # no holding scene on air (e.g. the host changed it): today's behavior
        self._chosen = scene
        if self.mode == "studio":
            await self.obs.request("SetCurrentPreviewScene", {"sceneName": scene})
        return True

    # -- live ------------------------------------------------------------------

    async def live_confirmed(self, live_id: str, stream_dir: Any, pressed_at: float | None) -> None:
        scene = self._chosen
        if self.mode == "studio":
            preview = await self._preview()
            if preview and preview not in HOLDING_SCENES:
                scene = preview
        await self._camera_on_air(scene)

    async def live_resumed(self, live_id: str, stream_dir: Any) -> None:
        program = await self._program()
        if program in HOLDING_SCENES or await self._studio_mode():
            await self._camera_on_air(self._chosen if program in HOLDING_SCENES else program)

    async def _camera_on_air(self, scene: str) -> None:
        # Studio Mode off first (the program stays): no preview/program swap afterwards.
        if await self._studio_mode():
            await self.obs.request("SetStudioModeEnabled", {"studioModeEnabled": False})
        await self.obs.request("SetCurrentProgramScene", {"sceneName": scene})
        log.info("holding: %r on air", scene)

    # -- OBS reads -------------------------------------------------------------

    async def _program(self) -> str | None:
        data = await self.obs.request("GetCurrentProgramScene") or {}
        return data.get("currentProgramSceneName") or data.get("sceneName")

    async def _preview(self) -> str | None:
        try:
            data = await self.obs.request("GetCurrentPreviewScene") or {}
        except Exception:
            return None  # Studio Mode is off
        return data.get("currentPreviewSceneName") or data.get("sceneName")

    async def _transition_s(self) -> float:
        """How long the current scene transition takes (plus a margin), at most 2 s."""
        try:
            data = await self.obs.request("GetCurrentSceneTransition") or {}
        except Exception:
            data = {}
        ms = data.get("transitionDuration")
        ms = ms if isinstance(ms, (int, float)) and not isinstance(ms, bool) else 300
        return min(2.0, ms / 1000 + 0.2)

    async def _studio_mode(self) -> bool:
        data = await self.obs.request("GetStudioModeEnabled") or {}
        return bool(data.get("studioModeEnabled"))
