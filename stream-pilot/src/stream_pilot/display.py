"""Keep every connected display mirroring the primary one.

The stream PC drives a display plus the NanoKVM's HDMI capture, and Plasma extends
the desktop onto a newly connected output. The window layout (the streampilotlayout
KWin script) wants one screen at 0,0, and the host and the KVM must see the same picture, so
``stream-pilot display-mirror`` (a user service) re-applies mirroring with
kscreen-doctor whenever the outputs change, with the largest display as the source.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

POLL_S = 3.0
_KSCREEN_TIMEOUT_S = 10.0


def native_area(output: dict) -> int:
    """Pixels of the output's preferred (native) mode, else its current mode, else 0."""
    modes = {m.get("id"): m for m in output.get("modes") or [] if isinstance(m, dict)}
    for mode_id in [*(output.get("preferredModes") or []), output.get("currentModeId")]:
        size = (modes.get(mode_id) or {}).get("size") or {}
        if size.get("width") and size.get("height"):
            return int(size["width"]) * int(size["height"])
    return 0


def _source(outputs: list[dict]) -> dict:
    """The output the others mirror: the largest native mode first (the desktop takes
    the source's size, and the layout needs 1920x1080: seen in situ, the KVM's HDMI
    dropped for a moment and the 1600x900 monitor stayed the source afterwards, so OBS
    no longer fit). Among equals, one that mirrors nothing itself (KWin may give a
    replica priority 1, so priority alone would flip an existing mirror into a cycle),
    then the lowest positive priority, then the lowest id."""
    largest = max(native_area(o) for o in outputs)
    outputs = [o for o in outputs if native_area(o) == largest]
    candidates = [o for o in outputs if not o.get("replicationSource")] or outputs
    ranked = [o for o in candidates if (o.get("priority") or 0) > 0]
    if ranked:
        return min(ranked, key=lambda o: (o["priority"], o["id"]))
    return min(candidates, key=lambda o: o["id"])


def mirror_args(outputs: list[dict]) -> list[str]:
    """kscreen-doctor arguments that make every connected output mirror the source at 0,0.

    ``outputs`` is ``kscreen-doctor -j``'s ``outputs`` list. Empty when nothing is to do.
    """
    connected = [o for o in outputs if o.get("connected")]
    if len(connected) < 2:
        return []
    # Disabled outputs count too (they are enabled below): a display that just came back
    # becomes the source in this pass if it is the largest.
    src = _source(connected)
    args: list[str] = []
    if not src.get("enabled"):
        args.append(f"output.{src['name']}.enable")
    if src.get("replicationSource"):  # every output mirrors another: a cycle
        args.append(f"output.{src['name']}.mirror.none")
    pos = src.get("pos") or {}
    if (pos.get("x"), pos.get("y")) != (0, 0):
        args.append(f"output.{src['name']}.position.0,0")
    for o in sorted(connected, key=lambda o: o["id"]):
        if o is src:
            continue
        if not o.get("enabled"):
            args.append(f"output.{o['name']}.enable")
        if o.get("replicationSource") != src["id"]:
            args.append(f"output.{o['name']}.mirror.{src['name']}")
    return args


def _kscreen(*args: str) -> str:
    return subprocess.run(
        ["kscreen-doctor", *args],
        check=True, capture_output=True, text=True, timeout=_KSCREEN_TIMEOUT_S,
    ).stdout


def run_display_mirror(
    *,
    kscreen: Callable[..., str] = _kscreen,
    sleep: Callable[[float], Any] = time.sleep,
    poll_s: float = POLL_S,
    rounds: int | None = None,
) -> int:
    """Poll the outputs and re-apply mirroring when needed (forever unless ``rounds``)."""
    n = 0
    while rounds is None or n < rounds:
        n += 1
        try:
            args = mirror_args(json.loads(kscreen("-j")).get("outputs") or [])
            if args:
                log.info("mirroring displays: kscreen-doctor %s", " ".join(args))
                kscreen(*args)
        except Exception as exc:  # no session yet, kscreen-doctor hiccup: try again later
            log.warning("display mirroring check failed: %r", exc)
        sleep(poll_s)
    return 0
