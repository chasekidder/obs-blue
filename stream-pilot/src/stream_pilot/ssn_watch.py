"""Self-heal trigger for SSN: watch the session bus for its "background stopped" notification.

SSN posts a Chromium desktop notification (summary
:data:`~stream_pilot.browser.SSN_STALLED_SUMMARY`) when its pinned background
tab is stuck and its own recovery failed; comment capture through SSN is down
until that tab is reopened. Do Not Disturb hides the popup, but Chromium still
sends the Notify call over the session bus, so ``dbus-monitor`` (in the image)
sees it. Each one becomes an :class:`~stream_pilot.events.SsnStalled` event.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Callable

from .browser import SSN_STALLED_SUMMARY
from .events import SsnStalled
from .ports import EventSink

log = logging.getLogger(__name__)

DBUS_MONITOR_ARGV = (
    "dbus-monitor",
    "--session",
    "interface=org.freedesktop.Notifications,member=Notify",
)
RETRY_S = 10
REAP_TIMEOUT_S = 5
_SUMMARY_LINE = f'string "{SSN_STALLED_SUMMARY}"'


async def _spawn(*argv: str) -> Any:
    return await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )


class SsnStallWatcher:
    """Runs ``dbus-monitor`` forever (restarting it if it exits); ``start()`` never returns."""

    def __init__(
        self,
        *,
        spawn: Callable[..., Awaitable[Any]] = _spawn,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._spawn = spawn
        self._sleep = sleep
        self._sink: EventSink = lambda _event: None

    def set_event_sink(self, sink: EventSink) -> None:
        self._sink = sink

    async def start(self) -> None:
        while True:
            try:
                proc = await self._spawn(*DBUS_MONITOR_ARGV)
            except OSError as exc:
                log.warning("cannot start dbus-monitor (%s); retrying in %ss", exc, RETRY_S)
                await self._sleep(RETRY_S)
                continue
            try:
                await self._read(proc)
            finally:
                await self._reap(proc)
            log.warning("dbus-monitor exited; restarting in %ss", RETRY_S)
            await self._sleep(RETRY_S)

    async def _read(self, proc: Any) -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            if line.decode(errors="replace").strip() == _SUMMARY_LINE:
                log.info("SSN reported that its background page stopped")
                self._sink(SsnStalled())

    @staticmethod
    async def _reap(proc: Any) -> None:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), REAP_TIMEOUT_S)
