"""System adapters: SystemCtl (SystemPort) and V4l2Camera (CameraPort).

Both run an external command via an injectable `exec_fn`
(default `asyncio.create_subprocess_exec`). stdout/stderr are sent to DEVNULL
(we only care about the exit code, and PIPE without draining could block).
A non-zero exit or a missing binary is logged at WARNING and never raised:
a failed autofocus tweak or unit stop must not crash the controller.
"""

import asyncio
import logging
import subprocess
from typing import Any, Callable

log = logging.getLogger(__name__)


async def _run(exec_fn: Callable[..., Any], argv: list[str]) -> int | None:
    """Run argv, return the exit code, or None if it could not be started."""
    try:
        proc = await exec_fn(
            *argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        code = await proc.wait()
    except OSError as e:  # FileNotFoundError, PermissionError, ...
        log.warning("cannot run %s: %s", argv, e)
        return None
    if code != 0:
        log.warning("%s exited with status %s", argv, code)
    return code


class SystemCtl:
    def __init__(self, *, exec_fn: Callable[..., Any] = asyncio.create_subprocess_exec):
        self._exec = exec_fn

    async def poweroff(self) -> None:
        await _run(self._exec, ["systemctl", "poweroff"])

    async def stop_user_unit(self, unit: str) -> None:
        await _run(self._exec, ["systemctl", "--user", "stop", unit])


class V4l2Camera:
    def __init__(self, *, exec_fn: Callable[..., Any] = asyncio.create_subprocess_exec):
        self._exec = exec_fn

    async def set_autofocus(self, device: str, on: bool) -> None:
        value = 1 if on else 0
        await _run(
            self._exec,
            ["v4l2-ctl", "-d", device, f"--set-ctrl=focus_automatic_continuous={value}"],
        )
