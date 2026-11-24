"""Per-stream metadata: ``<stream_dir>/stream.json`` and the ``DONE`` marker.

``stream.json`` is one JSON object, rewritten atomically whenever a field is
added (every field is optional, so a reader must not require any). Times are
Unix seconds (wall clock). Fields:

* ``live_id``, ``mic_device``, ``fb_delay_s``, ``replay_includes_preroll``
* ``preroll_started_at``: when OBS started the pre-roll stream (OBS's output
  duration at go-live, subtracted from the wall clock)
* ``go_live_pressed_at``, ``live_confirmed_at``, ``ended_at``
* ``resumed_at``: list of times stream-pilot resumed this live after a restart
* ``image``: ``{"digest", "version", "reference"}`` of the booted bootc image
* ``stream_pilot_version``
* ``clock_at_live`` / ``clock_at_end``: ``{"ntp_synchronized", "offset_s"}``
  (``offset_s`` is chrony's estimate of the system clock's error; positive =
  system clock behind true time)

``DONE`` is written last, at the very end of a stream: a folder without it is
still being written (or was cut short by a power cut).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

from .persist import _atomic_write_json

log = logging.getLogger(__name__)

STREAM_JSON = "stream.json"
DONE = "DONE"
CMD_TIMEOUT_S = 3.0


def read(stream_dir: Path) -> dict:
    try:
        data = json.loads((Path(stream_dir) / STREAM_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update(stream_dir: Path, **fields: Any) -> dict:
    """Merge ``fields`` (None values are skipped) into stream.json; return the result."""
    data = read(stream_dir)
    data.update({k: v for k, v in fields.items() if v is not None})
    _atomic_write_json(Path(stream_dir) / STREAM_JSON, data)
    return data


def append_to(stream_dir: Path, key: str, value: Any) -> None:
    data = read(stream_dir)
    items = data.get(key)
    data[key] = [*(items if isinstance(items, list) else []), value]
    _atomic_write_json(Path(stream_dir) / STREAM_JSON, data)


def write_done(stream_dir: Path, at: float) -> None:
    path = Path(stream_dir) / DONE
    tmp = path.with_name(DONE + ".tmp")
    tmp.write_text(f"{at:.3f}\n", encoding="utf-8")
    tmp.replace(path)


def package_version() -> str | None:
    try:
        return metadata.version("stream-pilot")
    except metadata.PackageNotFoundError:
        return None


async def _output(exec_fn: Callable[..., Any], argv: list[str]) -> str | None:
    try:
        proc = await exec_fn(*argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.info("cannot run %s: %s", argv[0], exc)
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), CMD_TIMEOUT_S)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        log.info("%s timed out", argv[0])
        return None
    if proc.returncode != 0:
        return None
    return out.decode(errors="replace")


async def image_info(exec_fn: Callable[..., Any] = asyncio.create_subprocess_exec) -> dict | None:
    """The booted bootc image (``rpm-ostree status`` works without root)."""
    out = await _output(exec_fn, ["rpm-ostree", "status", "--json", "--booted"])
    if out is None:
        return None
    try:
        deployment = json.loads(out)["deployments"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return {
        "digest": deployment.get("container-image-reference-digest"),
        "version": deployment.get("version"),
        "reference": deployment.get("container-image-reference"),
    }


_CHRONY_SYSTEM_TIME = re.compile(r"^System time\s*:\s*([\d.]+) seconds (slow|fast)", re.M)


def parse_chrony_offset(text: str) -> float | None:
    """``System time : 0.001 seconds slow of NTP time`` -> +0.001 (slow = behind)."""
    m = _CHRONY_SYSTEM_TIME.search(text or "")
    if not m:
        return None
    value = float(m.group(1))
    return value if m.group(2) == "slow" else -value


async def clock_info(exec_fn: Callable[..., Any] = asyncio.create_subprocess_exec) -> dict:
    synced_out = await _output(exec_fn, ["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    chrony_out = await _output(exec_fn, ["chronyc", "-n", "tracking"])
    synced = None if synced_out is None else synced_out.strip() == "yes"
    return {"ntp_synchronized": synced, "offset_s": parse_chrony_offset(chrony_out or "")}
