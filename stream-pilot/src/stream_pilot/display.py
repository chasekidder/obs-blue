"""Keep every connected display mirroring the primary one.

The stream PC drives a display plus the NanoKVM's HDMI capture, and Plasma extends
the desktop onto a newly connected output. The window layout (kwinrulesrc) assumes
one 1920x1080 screen at 0,0, and the host and the KVM must see the same picture, so
``stream-pilot display-mirror`` (a user service) re-applies mirroring with
kscreen-doctor whenever the outputs change.
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


def _source(outputs: list[dict]) -> dict:
    """The output the others mirror: one that mirrors nothing itself if there is one
    (KWin may give a replica priority 1, so priority alone would flip an existing
    mirror into a cycle), then the lowest positive priority, then the lowest id."""
    candidates = [o for o in outputs if not o.get("replicationSource")] or outputs
    ranked = [o for o in candidates if (o.get("priority") or 0) > 0]
    if ranked:
        return min(ranked, key=lambda o: (o["priority"], o["id"]))
    return min(candidates, key=lambda o: o["id"])


def mirror_args(outputs: list[dict]) -> list[str]:
    """kscreen-doctor arguments that make every connected output mirror the primary at 0,0.

    ``outputs`` is ``kscreen-doctor -j``'s ``outputs`` list. Empty when nothing is to do.
    """
    connected = [o for o in outputs if o.get("connected")]
    if len(connected) < 2:
        return []
    src = _source([o for o in connected if o.get("enabled")] or connected)
    args: list[str] = []
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
