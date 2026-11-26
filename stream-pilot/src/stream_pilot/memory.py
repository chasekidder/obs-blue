"""Memory guard: sample /proc once a minute, journal it, light the spare key.

The target PC has 8 GB of RAM and must run OBS + Chromium + Plasma for 2-4 h
streams, so the guard logs ``MemAvailable``, memory PSI and the RSS of the
processes that matter (spec "Memory and performance plan" item 8) and shows
``MEM`` on the spare Stream Deck key (index 4) when memory runs low.

Sample shape (the ``dict`` returned by :func:`take_sample`; it is passed to
``on_sample`` -> ``status.json`` and logged as one JSON line to the journal)::

    {
      "mem_available_mb": 2048,                       # MemAvailable kB // 1024
      "psi": {"some_avg60": 1.23, "full_avg60": 0.4}, # or null if no PSI file
      "procs": [                                      # sorted by pid
        {"pid": 201, "comm": "chromium", "rss_kb": 1843200, "rss_mb": 1800,
         "label": "chromium-renderer", "client_id": "7"},   # client_id optional
        ...
      ]
    }

``label`` is ``obs`` / ``kwin_wayland`` / ``plasmashell`` for those programs,
and for Chromium ``chromium-renderer`` (``--type=renderer``), ``chromium-<type>``
(any other ``--type=X``) or ``chromium-browser`` (no ``--type``). The spec's
"labeled by URL" is not available from ``/proc``; the closest thing is the
renderer's ``--renderer-client-id=N`` switch, reported as the optional
``client_id`` string when present.

``auto_reload_popout`` is read from the config but deliberately not acted on
here (the host must never lose comments); a later plan wires it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import MemoryCfg
from .ports import DeckPort
from .states import KeyFace

log = logging.getLogger("stream_pilot.memory")

SPARE_KEY = 4
MEM_FACE = KeyFace("MEM", "#e67e00", blink=True)
BLANK_FACE = KeyFace("", "#000000")

_WATCHED_COMMS = {"obs", "kwin_wayland", "plasmashell"}
_RENDERER = "chromium-renderer"


@dataclass(frozen=True)
class ProcSample:
    pid: int
    comm: str
    rss_kb: int
    label: str
    client_id: str | None = None  # Chromium --renderer-client-id=, when present


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse ``/proc/meminfo`` into ``{field: value}`` (kB, or a bare count)."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        parts = rest.split()
        if not sep or not parts:
            continue
        try:
            out[name.strip()] = int(parts[0])
        except ValueError:
            continue
    return out


def parse_psi(text: str) -> dict[str, float]:
    """Parse ``/proc/pressure/memory`` into the 60 s averages.

    A missing ``some``/``full`` line yields ``0.0`` for that value.
    """
    out = {"some_avg60": 0.0, "full_avg60": 0.0}
    for line in text.splitlines():
        kind, _, rest = line.strip().partition(" ")
        if kind not in ("some", "full"):
            continue
        for field in rest.split():
            key, _, val = field.partition("=")
            if key == "avg60":
                try:
                    out[f"{kind}_avg60"] = float(val)
                except ValueError:
                    pass
    return out


def _chromium_label(argv: list[str]) -> tuple[str, str | None]:
    kind = None
    client_id = None
    # Chromium's child processes rewrite their command line into one string.
    args = [a for part in argv for a in part.split(" ")]
    for arg in args:
        if arg.startswith("--type="):
            kind = arg[len("--type="):]
        elif arg.startswith("--renderer-client-id="):
            client_id = arg[len("--renderer-client-id="):]
    if kind == "renderer":
        return _RENDERER, client_id
    return (f"chromium-{kind}" if kind else "chromium-browser"), client_id


def _vmrss_kb(status: str) -> int | None:
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def sample_processes(proc_root: Path = Path("/proc")) -> list[ProcSample]:
    """RSS of OBS, KWin, Plasma and every Chromium process, sorted by pid.

    Pids that vanish or can't be read mid-scan are skipped silently, as are
    kernel threads (no ``VmRSS``) and every unrelated process.
    """
    try:
        entries = [e for e in proc_root.iterdir() if e.name.isdigit()]
    except OSError:
        return []
    out: list[ProcSample] = []
    for entry in sorted(entries, key=lambda e: int(e.name)):
        try:
            comm = (entry / "comm").read_text(errors="replace").strip()
            chromium = comm.startswith(("chromium", "chrome"))
            if comm not in _WATCHED_COMMS and not chromium:
                continue
            rss_kb = _vmrss_kb((entry / "status").read_text(errors="replace"))
            if rss_kb is None:
                continue
            label, client_id = comm, None
            if chromium:
                try:
                    raw = (entry / "cmdline").read_bytes()
                except OSError:
                    raw = b""
                argv = raw.decode(errors="replace").split("\0")
                label, client_id = _chromium_label(argv)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        out.append(ProcSample(int(entry.name), comm, rss_kb, label, client_id))
    return out


def take_sample(proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """One memory sample (see the module docstring for the exact shape).

    Raises ``OSError``/``KeyError`` if ``meminfo`` is unreadable or has no
    ``MemAvailable``; a missing PSI file just gives ``psi = None``.
    """
    info = parse_meminfo((proc_root / "meminfo").read_text())
    try:
        psi: dict[str, float] | None = parse_psi((proc_root / "pressure/memory").read_text())
    except OSError:
        psi = None
    procs = []
    for p in sample_processes(proc_root):
        entry: dict[str, Any] = {
            "pid": p.pid,
            "comm": p.comm,
            "rss_kb": p.rss_kb,
            "rss_mb": p.rss_kb // 1024,
            "label": p.label,
        }
        if p.client_id is not None:
            entry["client_id"] = p.client_id
        procs.append(entry)
    return {"mem_available_mb": info["MemAvailable"] // 1024, "psi": psi, "procs": procs}


def evaluate(sample: dict, cfg: MemoryCfg) -> str | None:
    """``"MEM"`` if available memory is under the floor or a renderer is huge."""
    if sample["mem_available_mb"] < cfg.floor_mb:
        return "MEM"
    for p in sample["procs"]:
        if p["label"] == _RENDERER and p["rss_mb"] > cfg.popout_rss_warn_mb:
            return "MEM"
    return None


_UNSET: Any = object()


class MemoryGuard:
    def __init__(
        self,
        cfg: MemoryCfg,
        deck: DeckPort,
        on_sample: Callable[[dict], None],
        *,
        sampler: Callable[[], dict] = take_sample,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._cfg = cfg
        self._deck = deck
        self._on_sample = on_sample
        self._sampler = sampler
        self._sleep = sleep
        self._last: Any = _UNSET  # last result shown on the key

    def _step(self) -> None:
        try:
            sample = self._sampler()
        except Exception as exc:
            log.warning("memory sampling failed: %s", exc)
            return
        log.info("%s", json.dumps(sample, separators=(",", ":"), sort_keys=True))
        try:
            self._on_sample(sample)
        except Exception as exc:
            log.error("memory on_sample callback failed: %s", exc, exc_info=True)
        result = evaluate(sample, self._cfg)
        if result == self._last:
            return
        try:
            self._deck.show(SPARE_KEY, MEM_FACE if result == "MEM" else BLANK_FACE)
        except Exception as exc:
            log.error("memory guard could not update the deck: %s", exc, exc_info=True)
            return  # leave _last unchanged so the next iteration retries
        self._last = result

    async def run(self) -> None:
        """Sample, log, notify and update key 4 every ``interval_s`` until cancelled."""
        while True:
            self._step()
            await self._sleep(self._cfg.interval_s)
