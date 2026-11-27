"""Match a configured OBS scene name against the scenes OBS really has.

The host renames scenes in OBS (2026-11-26: "Jewlery Camera" became "Jewelry
Camera" while the config kept the old spelling, and every JEWEL CAM press
failed). A configured name is used as is if OBS has it; otherwise the one
scene that matches ignoring case and spacing, else the one scene within two
edits of it. Anything less certain is no match.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

log = logging.getLogger(__name__)

MAX_EDITS = 2


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def _edits(a: str, b: str) -> int:
    """Levenshtein distance (small strings only)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def resolve_scene(wanted: str, available: Iterable[str]) -> str | None:
    names = [n for n in available if isinstance(n, str)]
    if wanted in names:
        return wanted
    w = _norm(wanted)
    same = [n for n in names if _norm(n) == w]
    if len(same) == 1:
        return same[0]
    near = [n for n in names if _edits(_norm(n), w) <= MAX_EDITS]
    return near[0] if len(near) == 1 else None


async def obs_scene_names(obs: Any) -> list[str]:
    data = await obs.request("GetSceneList") or {}
    return [s.get("sceneName") for s in data.get("scenes") or [] if isinstance(s, dict)]
