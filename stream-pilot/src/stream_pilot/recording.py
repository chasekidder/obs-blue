"""``[recording]``: record the microphone while live; stream.json and DONE.

The recorder itself is ``stream-pilot record`` in its own user unit
(``stream-pilot-recorder@<stream dir>.service``, see recorder.py), so a
stream-pilot crash or restart never interrupts it. This feature starts it when
Facebook confirms the live, starts it again if stream-pilot resumes a live and
finds it gone (the recorder lists the gap), and stops it gracefully once the
stream has stopped, before the comment capture stops (the unit sends SIGINT to
the recorder only, which passes one on to ffmpeg). It also writes the stream's ``stream.json`` and, last, ``DONE``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable

from . import stream_meta
from .config import Config
from .features import Feature
from .system import escape_unit_path

log = logging.getLogger(__name__)

RECORDER_UNITS = "stream-pilot-recorder@*.service"


def recorder_unit(stream_dir: Path) -> str:
    return f"stream-pilot-recorder@{escape_unit_path(str(stream_dir))}.service"


class Recording(Feature):
    name = "recording"

    def __init__(
        self,
        cfg: Config,
        *,
        obs: Any,
        system: Any,
        clock: Callable[[], float] = time.time,
        exec_fn: Callable[..., Any] = asyncio.create_subprocess_exec,
    ):
        super().__init__(cfg.recording.enabled)
        self.cfg = cfg
        self.obs = obs
        self.system = system
        self._clock = clock
        self._exec = exec_fn
        self._image: dict | None = None

    async def restore(self) -> None:
        await self.system.stop_user_unit(RECORDER_UNITS)

    async def obs_connected(self) -> None:
        if self._image is None:
            self._image = await stream_meta.image_info(self._exec)

    async def not_live(self) -> None:
        """No live (boot without a live to resume): a recorder left running is stale."""
        await self.system.stop_user_unit(RECORDER_UNITS)

    async def live_confirmed(self, live_id: str, stream_dir: Path, pressed_at: float | None) -> None:
        confirmed_at = self._clock()
        await self.system.stop_user_unit(RECORDER_UNITS)
        if not await self.system.start_user_unit(recorder_unit(stream_dir)):
            log.warning("recording: the recorder did not start")
        rec = self.cfg.recording
        stream_meta.update(
            stream_dir,
            live_id=live_id,
            go_live_pressed_at=pressed_at,
            live_confirmed_at=confirmed_at,
            mic_device=self.cfg.audio.mic_device,
            fb_delay_s=rec.fb_delay_s,
            replay_includes_preroll=rec.replay_includes_preroll,
            stream_pilot_version=stream_meta.package_version(),
        )
        stream_meta.update(stream_dir, preroll_started_at=await self._preroll_started_at())
        if self._image is None:
            self._image = await stream_meta.image_info(self._exec)
        stream_meta.update(
            stream_dir, image=self._image, clock_at_live=await stream_meta.clock_info(self._exec)
        )

    async def live_resumed(self, live_id: str, stream_dir: Path) -> None:
        stream_meta.append_to(stream_dir, "resumed_at", self._clock())
        stream_meta.update(stream_dir, live_id=live_id, mic_device=self.cfg.audio.mic_device)
        unit = recorder_unit(stream_dir)
        if await self.system.user_unit_active(unit):
            return
        log.warning("recording: the recorder was not running; starting it (the gap is listed)")
        if not await self.system.start_user_unit(unit):
            log.warning("recording: the recorder did not start")

    async def stream_stopped(self, stream_dir: Path, ended_at: float) -> None:
        stream_meta.update(stream_dir, ended_at=ended_at)
        await self.system.stop_user_unit(recorder_unit(stream_dir))

    async def stream_closed(self, stream_dir: Path) -> None:
        stream_meta.update(stream_dir, clock_at_end=await stream_meta.clock_info(self._exec))

    async def stream_done(self, stream_dir: Path) -> None:
        stream_meta.write_done(stream_dir, self._clock())

    async def _preroll_started_at(self) -> float | None:
        try:
            status = await self.obs.request("GetStreamStatus")
        except Exception as exc:
            log.info("recording: GetStreamStatus failed: %r", exc)
            return None
        duration_ms = (status or {}).get("outputDuration")
        if not isinstance(duration_ms, (int, float)) or isinstance(duration_ms, bool):
            return None
        return self._clock() - duration_ms / 1000.0
