"""Optional features, each behind its own ``[<name>] enabled`` switch.

A feature is an object the controller calls at fixed points of the stream's
life. Switches are read once, at startup (from the loaded config). The
controller only calls the hooks of features that are on, except
:meth:`Feature.restore`, which it calls for features that are off, at boot
(unless ``restore_needs_obs``) and on every OBS connect, so a feature that was on earlier undoes what it changed
(camera controls back to auto, its OBS items disabled or hidden).

Every hook is awaited with a timeout and its exceptions are logged, never
raised: a broken feature must never put the controller into ERROR, block
READY or Go Live, or stretch the end flow.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Iterable

log = logging.getLogger(__name__)

# Default bound for one feature hook (end-flow hooks get the end flow's budget).
HOOK_TIMEOUT_S = 10.0


class Feature:
    name: str = ""
    # restore() only talks to OBS: it runs on OBS connects, not at boot (OBS may not be up yet).
    restore_needs_obs: bool = False

    def __init__(self, enabled: bool):
        self.enabled = enabled

    async def restore(self) -> None:
        """The switch is off: undo anything an earlier run changed (idempotent)."""

    async def obs_connected(self) -> None:
        """OBS (re)connected: create the feature's missing OBS items and enable them."""


_DEFAULT = object()


async def run_hook(
    feature: Feature, hook: str, aw_factory: Callable[[], Awaitable[object]],
    timeout: float | None | object = _DEFAULT,
) -> bool:
    """Await one hook, bounded (default HOOK_TIMEOUT_S, read at call time) and best
    effort; True if it finished without error."""
    if timeout is _DEFAULT:
        timeout = HOOK_TIMEOUT_S
    try:
        if timeout is None:
            await aw_factory()
        else:
            await asyncio.wait_for(aw_factory(), timeout)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        log.warning("feature %s: %s timed out after %.1fs (continuing)", feature.name, hook, timeout)
        return False
    except Exception:
        log.exception("feature %s: %s failed (continuing)", feature.name, hook)
        return False
    return True


def describe(switches: dict[str, bool]) -> str:
    """``"recording on, upload off"`` (``"-"`` when there are none)."""
    return ", ".join(f"{name} {'on' if on else 'off'}" for name, on in switches.items()) or "-"


def enabled(features: Iterable[Feature]) -> list[Feature]:
    return [f for f in features if f.enabled]
