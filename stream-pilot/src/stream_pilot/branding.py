"""``[branding]``: a watermark, an outro card before the stream stops, the live's title.

* **Watermark:** a small text source ("Branding Watermark", a placeholder for a
  logo) in a corner of every camera scene, created over obs-websocket if
  missing; restyle or replace it in OBS.
* **Outro:** at the end of a live (power button, two status-key presses, ``stream-pilot
  press --long``), the "Outro" scene (a card with ``outro_text``, no
  microphone) is on air for ``outro_s`` seconds before StopStream, while the
  status key counts down. Afterwards the scene that was on air is put back, so
  the next pre-roll starts from a camera scene.
* **Title:** ``[facebook] live_title_template`` is typed into the producer's
  post details before every pre-roll starts.

Off: none of this runs; the restore hides the watermark and the outro card.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from .config import Config
from .features import Feature
from .obs_items import Item, Scene, SceneItem, apply_items
from .scenes import obs_scene_names, resolve_scene
from .states import STATUS_FACES, KeyFace, State

log = logging.getLogger(__name__)

WATERMARK = "Branding Watermark"
OUTRO_SCENE = "Outro"
# The outro happens before the stream stops; it must leave the end flow time to finish.
OUTRO_MAX_S = 15.0
CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
_W, _H, _MARGIN = 1080, 1920, 40
_ALIGN = {"top-left": 5, "top-right": 6, "bottom-left": 9, "bottom-right": 10}
_WATERMARK_WHITE = 0xB3FFFFFF  # 70 % opaque
_WHITE = 0xFFFFFFFF
_OUTRO_FACE_BG = "#c0141c"


def watermark_transform(corner: str) -> dict:
    x = _MARGIN if corner.endswith("left") else _W - _MARGIN
    y = _MARGIN if corner.startswith("top") else _H - _MARGIN
    return {"positionX": float(x), "positionY": float(y), "alignment": _ALIGN[corner]}


def live_title(template: str, group_name: str, now: datetime) -> str | None:
    try:
        title = template.format(group_name=group_name, date=now).strip()
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        log.warning("branding: cannot fill live_title_template %r: %s", template, exc)
        return None
    return title or None


class Branding(Feature):
    name = "branding"
    restore_needs_obs = True

    def __init__(
        self,
        cfg: Config,
        *,
        obs: Any,
        fb: Any,
        deck: Any,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = datetime.now,
    ):
        super().__init__(cfg.branding.enabled)
        self.cfg = cfg
        self.obs = obs
        self.fb = fb
        self.deck = deck
        self._sleep = sleep
        self._now = now
        b = cfg.branding
        self.corner = b.watermark_corner if b.watermark_corner in CORNERS else "top-right"
        if b.watermark_corner not in CORNERS:
            log.warning("branding: unknown watermark_corner %r; using top-right", b.watermark_corner)
        self.outro_s = max(0.0, min(float(b.outro_s), OUTRO_MAX_S))
        self._before_outro: str | None = None

    def items(self, scenes: list[str] | None = None) -> list[Item]:
        """``scenes``: OBS's scene names; a camera scene OBS does not have is skipped
        (or matched to OBS's spelling of it, see scenes.resolve_scene)."""
        b = self.cfg.branding
        out: list[Item] = []
        for i, cam in enumerate(self.cfg.cameras.values()):
            scene = cam.scene if scenes is None else resolve_scene(cam.scene, scenes)
            if scene is None:
                continue
            out.append(SceneItem(
                scene, WATERMARK,
                kind="text_ft2_source_v2" if i == 0 else None,
                settings={"text": b.watermark_text, "color1": _WATERMARK_WHITE,
                          "color2": _WATERMARK_WHITE, "outline": True,
                          "font": {"face": "DejaVu Sans", "size": 44, "style": "Bold", "flags": 1}},
                transform=watermark_transform(self.corner),
            ))
        out += [
            Scene(OUTRO_SCENE),
            SceneItem(OUTRO_SCENE, "Outro Background", kind="color_source_v3",
                      settings={"color": 0xFF202020, "width": _W, "height": _H},
                      transform={"positionX": 0.0, "positionY": 0.0, "alignment": 5}),
            SceneItem(OUTRO_SCENE, "Outro Text", kind="text_ft2_source_v2",
                      settings={"text": b.outro_text, "color1": _WHITE, "color2": _WHITE,
                                "align": "center",
                                "font": {"face": "DejaVu Sans", "size": 84, "style": "Bold",
                                         "flags": 1}},
                      transform={"positionX": _W / 2, "positionY": _H / 2, "alignment": 0}),
        ]
        return out

    async def obs_connected(self) -> None:
        try:
            scenes: list[str] | None = await obs_scene_names(self.obs)
        except Exception as exc:
            log.warning("branding: cannot list OBS's scenes: %r", exc)
            scenes = None
        await apply_items(self.obs, self.items(scenes), True, owner="branding")

    async def restore(self) -> None:
        await apply_items(self.obs, self.items(), False, owner="branding")
        data = await self.obs.request("GetCurrentProgramScene") or {}
        main = self.cfg.cameras.get("main")
        if (data.get("currentProgramSceneName") or data.get("sceneName")) == OUTRO_SCENE and main:
            await self.obs.request("SetCurrentProgramScene", {"sceneName": main.scene})

    # -- title -------------------------------------------------------------------

    async def before_preroll(self) -> None:
        fb = self.cfg.facebook
        title = live_title(fb.live_title_template, fb.group_name, self._now())
        if title:
            await self.fb.set_title(title)

    # -- outro -------------------------------------------------------------------

    async def before_stop_stream(self) -> None:
        if self.outro_s <= 0:
            return
        await apply_items(self.obs, self.items(), True, owner="branding")
        data = await self.obs.request("GetCurrentProgramScene") or {}
        self._before_outro = data.get("currentProgramSceneName") or data.get("sceneName")
        await self.obs.request("SetCurrentProgramScene", {"sceneName": OUTRO_SCENE})
        log.info("branding: outro on air for %gs", self.outro_s)
        try:
            left = self.outro_s
            while left > 0:
                self._show(KeyFace(f"OUTRO\n{int(left + 0.999)}", _OUTRO_FACE_BG))
                step = min(1.0, left)
                await self._sleep(step)
                left -= step
        finally:
            self._show(STATUS_FACES[State.ENDING])

    async def stream_stopped(self, stream_dir: Any, ended_at: float) -> None:
        scene, self._before_outro = self._before_outro, None
        if scene and scene != OUTRO_SCENE:
            await self.obs.request("SetCurrentProgramScene", {"sceneName": scene})

    def _show(self, face: KeyFace) -> None:
        try:
            self.deck.show(0, face)
        except Exception:
            log.exception("branding: cannot update the status key")
