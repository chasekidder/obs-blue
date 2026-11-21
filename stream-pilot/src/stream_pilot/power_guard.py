"""Power-button guard: a press always ends in a power-off, even if stream-pilot is hung.

Runs as root in stream-pilot-power-guard.service, outside the user session. A
power-button press always means "power off" (after a clean end if live). If, by
``DEADLINE_S`` after a press, stream-pilot hasn't powered the PC off and isn't
ENDING a live, the guard runs ``systemctl poweroff`` itself. A fresh ENDING gets
``GRACE_S`` more to stop the stream, let Facebook end the live and write the
report.

It reacts to presses, not holds: the stream PC's ACPI power button reports key
down and up at the same instant however long it is held, so a hold can't be
detected (holding ~4 s triggers the firmware's unclean power cut instead).
"""

from __future__ import annotations

import json
import logging
import pwd
import select
import subprocess
import time
from pathlib import Path
from typing import Callable

from .power import _close, _ecodes, find_power_devices

log = logging.getLogger(__name__)

DEADLINE_S = 15.0
GRACE_S = 90.0
FRESH_S = 30.0  # status.json is rewritten on every transition and memory sample
POLL_S = 0.5


def ending_in_progress(status_path: Path, now: float) -> bool:
    """True if stream-pilot's status file says ENDING and was written recently."""
    try:
        data = json.loads(Path(status_path).read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("state") != "ENDING":
        return False
    updated = data.get("updated_at")
    return isinstance(updated, (int, float)) and now - updated <= FRESH_S


class PressGuard:
    """Turns power-button presses and clock ticks (monotonic seconds) into a power-off."""

    def __init__(
        self,
        ending: Callable[[], bool],
        poweroff: Callable[[], bool],
        *,
        deadline_s: float = DEADLINE_S,
        grace_s: float = GRACE_S,
    ):
        self._ending = ending
        self._poweroff = poweroff
        self.deadline_s = deadline_s
        self.grace_s = grace_s
        self._check_at: float | None = None
        self._grace_until: float | None = None
        self._done = False

    def press(self, now: float) -> None:
        if self._done or self._check_at is not None or self._grace_until is not None:
            return
        log.info("power button pressed; powering off in %.0f s unless stream-pilot does", self.deadline_s)
        self._check_at = now + self.deadline_s

    def tick(self, now: float) -> None:
        if self._grace_until is not None and now >= self._grace_until:
            log.warning("stream-pilot did not finish ending in %.0f s; powering off", self.grace_s)
            self._fire()
        elif self._check_at is not None and now >= self._check_at:
            self._check_at = None
            if self._ending():
                log.info("stream-pilot is ending the live; waiting up to %.0f s", self.grace_s)
                self._grace_until = now + self.grace_s
            else:
                log.warning("still up %.0f s after a power-button press; powering off", self.deadline_s)
                self._fire()

    def _fire(self) -> None:
        self._grace_until = None
        self._done = self._poweroff()
        if not self._done:
            log.error("power-off failed; another press will retry")


def _systemctl_poweroff() -> bool:
    try:
        return subprocess.run(["systemctl", "poweroff"], check=False).returncode == 0
    except OSError as exc:
        log.error("cannot run systemctl poweroff: %s", exc)
        return False


def run_power_guard(user: str) -> int:
    """Read the Power Button devices until one fails (systemd restarts the unit)."""
    status_path = Path(pwd.getpwnam(user).pw_dir) / ".local/state/stream-pilot/status.json"
    devices = find_power_devices()
    if not devices:
        log.error("no power-button input device found")
        return 1
    ev_key, key_power = _ecodes()
    guard = PressGuard(lambda: ending_in_progress(status_path, time.time()), _systemctl_poweroff)
    log.info("watching %s", ", ".join(getattr(d, "path", "?") for d in devices))
    try:
        while True:
            readable, _, _ = select.select(devices, [], [], POLL_S)
            for dev in readable:
                for ev in dev.read():
                    if ev.type == ev_key and ev.code == key_power and ev.value == 1:
                        guard.press(time.monotonic())
            guard.tick(time.monotonic())
    except OSError as exc:
        log.error("power-button device lost: %s", exc)
        return 1
    finally:
        for dev in devices:
            _close(dev)
