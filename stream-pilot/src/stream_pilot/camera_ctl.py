"""V4L2 camera controls through ``v4l2-ctl`` (white balance and exposure).

The kernel renamed these controls in 5.x (``white_balance_temperature_auto``
→ ``white_balance_automatic``, ``exposure_auto`` → ``auto_exposure``,
``exposure_absolute`` → ``exposure_time_absolute``), so the names are read
from the device (``v4l2-ctl -l``) instead of assumed.

Measured on the C920 (docs/superpowers/notes/2026-11-24-hardware-checks.md):
turning auto white balance off makes it jump to the *stored* manual value,
and the value can only be written while auto is off.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

CMD_TIMEOUT_S = 3.0
AUTO_EXPOSURE_MANUAL = 1
AUTO_EXPOSURE_APERTURE_PRIORITY = 3

_NAMES = {
    "wb_auto": ("white_balance_automatic", "white_balance_temperature_auto"),
    "wb_temp": ("white_balance_temperature",),
    "exposure_auto": ("auto_exposure", "exposure_auto"),
    "exposure_abs": ("exposure_time_absolute", "exposure_absolute"),
}
_CTRL_LINE = re.compile(r"^\s*([a-z0-9_]+) 0x[0-9a-f]+ \((\w+)\)\s*:(.*)$")


@dataclass(frozen=True)
class Controls:
    """The device's names for each control (None: the camera does not have it)."""

    wb_auto: str | None = None
    wb_temp: str | None = None
    exposure_auto: str | None = None
    exposure_abs: str | None = None


def parse_controls(text: str) -> dict[str, dict[str, str]]:
    """``v4l2-ctl -l`` output -> {name: {"type", "value", "flags", ...}}."""
    out: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        m = _CTRL_LINE.match(line)
        if not m:
            continue
        fields = {"type": m.group(2)}
        for key, val in re.findall(r"(\w+)=([^\s]+(?: \([^)]*\))?)", m.group(3)):
            fields[key] = val
        out[m.group(1)] = fields
    return out


def detect(controls: dict[str, dict[str, str]]) -> Controls:
    found = {k: next((n for n in names if n in controls), None) for k, names in _NAMES.items()}
    return Controls(**found)


async def _run(exec_fn: Callable[..., Any], argv: list[str]) -> tuple[int | None, str]:
    try:
        proc = await exec_fn(*argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        log.warning("cannot run %s: %s", argv[0], exc)
        return None, ""
    try:
        out, err = await asyncio.wait_for(proc.communicate(), CMD_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        log.warning("%s timed out", " ".join(argv))
        return None, ""
    if proc.returncode != 0:
        log.warning("%s failed (%s): %s", " ".join(argv), proc.returncode,
                    err.decode(errors="replace").strip()[:200])
    return proc.returncode, out.decode(errors="replace")


class V4l2Controls:
    """Get and set one device's controls (each call is one bounded ``v4l2-ctl``)."""

    def __init__(self, device: str, *, exec_fn: Callable[..., Any] = asyncio.create_subprocess_exec):
        self.device = device
        self._exec = exec_fn
        self._names: Controls | None = None

    async def names(self) -> Controls:
        """The device's control names; empty (and quiet) while the camera is not plugged in."""
        if self._names is None:
            if not os.path.exists(self.device):
                return Controls()
            code, out = await _run(self._exec, ["v4l2-ctl", "-d", self.device, "-l"])
            if code != 0:
                return Controls()  # not cached: the camera may be plugged in later
            self._names = detect(parse_controls(out))
        return self._names

    async def get(self, name: str) -> int | None:
        code, out = await _run(self._exec, ["v4l2-ctl", "-d", self.device, f"--get-ctrl={name}"])
        if code != 0:
            return None
        value = out.rpartition(":")[2].strip().split(" ")[0]
        try:
            return int(value)
        except ValueError:
            return None

    async def set(self, **values: int) -> bool:
        arg = ",".join(f"{k}={v}" for k, v in values.items())
        code, _ = await _run(self._exec, ["v4l2-ctl", "-d", self.device, f"--set-ctrl={arg}"])
        return code == 0
