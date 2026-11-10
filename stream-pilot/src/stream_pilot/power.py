"""Power-key adapter: reads KEY_POWER from the evdev "Power Button" device(s).

logind is configured to ignore the power key, so stream-pilot reads it itself
and the controller ends the stream cleanly before powering off.

`evdev` is imported lazily (it is Fedora's python3-evdev in the image and is
absent from the host dev venv). The EV_KEY/KEY_POWER constants below are the
Linux input-event-codes values, used as fallbacks when evdev can't be imported
(tests) and are identical to `evdev.ecodes.EV_KEY` / `evdev.ecodes.KEY_POWER`.
"""

import asyncio
import logging
from typing import Any, Callable

from .events import PowerPress
from .ports import EventSink

log = logging.getLogger(__name__)

EV_KEY = 1
KEY_POWER = 116
POWER_BUTTON_NAME = "Power Button"
RETRY_NO_DEVICE_S = 10
RETRY_AFTER_LOSS_S = 1  # guards against a busy loop if a device keeps vanishing


def _ecodes() -> tuple[int, int]:
    try:
        from evdev import ecodes  # type: ignore[import-not-found]

        return ecodes.EV_KEY, ecodes.KEY_POWER
    except ImportError:
        return EV_KEY, KEY_POWER


def _close(dev: Any) -> None:
    try:
        dev.close()
    except Exception:  # noqa: BLE001 - closing is best-effort
        log.debug("closing input device failed", exc_info=True)


def find_power_devices(
    list_devices: Callable[[], list] | None = None,
    open_device: Callable[[str], Any] | None = None,
) -> list:
    """Return open devices named "Power Button" that can emit KEY_POWER.

    Non-matching (and failed-to-open) devices are skipped; non-matching devices
    that were opened are closed. Defaults resolve to evdev lazily.
    """
    if list_devices is None or open_device is None:
        import evdev  # type: ignore[import-not-found]

        list_devices = list_devices or evdev.list_devices
        open_device = open_device or evdev.InputDevice
    ev_key, key_power = _ecodes()
    found = []
    for path in list_devices():
        try:
            dev = open_device(path)
        except OSError as e:
            log.debug("cannot open %s: %s", path, e)
            continue
        try:
            ok = dev.name == POWER_BUTTON_NAME and key_power in dev.capabilities().get(
                ev_key, []
            )
        except OSError:
            ok = False
        if ok:
            found.append(dev)
        else:
            _close(dev)
    return found


class PowerKeyListener:
    """PowerPort implementation. `start()` runs forever."""

    def __init__(
        self,
        *,
        find_devices: Callable[[], list] = find_power_devices,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._find_devices = find_devices
        self._sleep = sleep
        self._sink: EventSink = lambda _event: None

    def set_event_sink(self, sink: EventSink) -> None:
        self._sink = sink

    async def start(self) -> None:
        while True:
            devices = self._find_devices()
            if not devices:
                log.warning(
                    "no power-button input device found; retrying in %ss",
                    RETRY_NO_DEVICE_S,
                )
                await self._sleep(RETRY_NO_DEVICE_S)
                continue
            await self._read_all(devices)
            await self._sleep(RETRY_AFTER_LOSS_S)

    async def _read_all(self, devices: list) -> None:
        """Read every device until one fails/ends, then clean up (-> rediscovery)."""
        ev_key, key_power = _ecodes()
        tasks = [asyncio.ensure_future(self._read(d, ev_key, key_power)) for d in devices]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for d in devices:
                _close(d)

    async def _read(self, dev: Any, ev_key: int, key_power: int) -> None:
        name = getattr(dev, "name", dev)
        try:
            async for event in dev.async_read_loop():
                if event.type == ev_key and event.code == key_power and event.value == 1:
                    self._sink(PowerPress())
        except OSError as e:
            log.warning("power-button device %s lost: %s", name, e)
            return
        log.warning("power-button device %s stopped producing events", name)
