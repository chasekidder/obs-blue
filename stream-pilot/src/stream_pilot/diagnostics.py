"""``[diagnostics]``: detailed measurements of every live, for reviewing the first streams.

The collector is ``stream-pilot diag <stream dir>`` in its own user unit
(``stream-pilot-diag@<stream dir>.service``). The unit runs at idle CPU and I/O
priority with a CPU quota and a memory cap, so it only ever gets time that OBS,
Chromium and the controller leave over; a collector that fails is only logged.
This feature starts it when Facebook confirms the live (again if stream-pilot
resumes a live and finds it gone) and stops it once the stream has stopped.

It writes ``<stream dir>/diag/`` (one JSON object per line, ``"t"`` = Unix s):

* ``obs.jsonl`` every ``obs_s``: OBS's frame counters (rendered, skipped for
  rendering lag, skipped for encoding lag, dropped by the network), render
  time, CPU, memory, the output's bytes, congestion and reconnect flag, and
  the program scene.
* ``network.jsonl`` every ``network_s``: byte rates of the default-route
  interface, its Wi-Fi link (signal, bitrates, retries, beacon loss), OBS's
  RTMP socket (``ss -ti``: RTT, retransmits, delivery rate, unsent bytes) and
  a ping to the default gateway.
* ``system.jsonl`` every ``system_s``: CPU busy %, load, memory, pressure
  (CPU/memory/I/O), CPU % and RSS per process group (OBS, Chromium, KWin,
  Plasma, Xwayland, PipeWire, stream-pilot, ffmpeg), temperatures, CPU and
  GPU clock.
* ``gateway.jsonl`` every ``gateway_s``, if ``gateway_status_url`` is set: the
  router's own status JSON (for the T-Mobile 5G gateway, its signal section).

When stopped (SIGTERM/SIGINT) it adds ``journal-user.txt`` and
``journal-system.txt`` (both journals from 5 minutes before the start),
the OBS log files written meanwhile (``obs-*.txt``) and ``summary.txt``.
Anything shaped like a Facebook stream key is replaced in the exported text.
The folder can still hold people's names (comments in the journal): keep it private.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import signal
import statistics
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import Config, DiagnosticsCfg
from .features import Feature
from .memory import _chromium_label, parse_meminfo
from .system import escape_unit_path

log = logging.getLogger(__name__)

DIAG_DIR = "diag"
DIAG_UNITS = "stream-pilot-diag@*.service"
JOURNAL_BACKLOG_S = 300.0
SUBPROCESS_TIMEOUT_S = 5.0
GATEWAY_TIMEOUT_S = 3.0
GATEWAY_MAX_BYTES = 64 * 1024
# A burst: frames skipped or dropped between two samples.
_KEY_RE = re.compile(r"FB-[A-Za-z0-9_-]{16,}")


def diag_unit(stream_dir: Path) -> str:
    return f"stream-pilot-diag@{escape_unit_path(str(stream_dir))}.service"


class Diagnostics(Feature):
    name = "diagnostics"

    def __init__(self, cfg: Config, *, system: Any):
        super().__init__(cfg.diagnostics.enabled)
        self.system = system

    async def restore(self) -> None:
        await self.system.stop_user_unit(DIAG_UNITS)

    async def not_live(self) -> None:
        await self.system.stop_user_unit(DIAG_UNITS)

    async def live_confirmed(self, live_id: str, stream_dir: Path, pressed_at: float | None) -> None:
        await self.system.stop_user_unit(DIAG_UNITS)
        if not await self.system.start_user_unit(diag_unit(stream_dir)):
            log.warning("diagnostics: the collector did not start")

    async def live_resumed(self, live_id: str, stream_dir: Path) -> None:
        unit = diag_unit(stream_dir)
        if await self.system.user_unit_active(unit):
            return
        log.warning("diagnostics: the collector was not running; starting it")
        if not await self.system.start_user_unit(unit):
            log.warning("diagnostics: the collector did not start")

    async def stream_stopped(self, stream_dir: Path, ended_at: float) -> None:
        await self.system.stop_user_unit_no_block(diag_unit(stream_dir))


# ---------------------------------------------------------------- parsers


def parse_proc_stat_cpu(text: str) -> tuple[int, int] | None:
    """``(busy, total)`` jiffies from the ``cpu`` line of /proc/stat."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            vals = [int(v) for v in line.split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals[:8])
            return total - idle, total
    return None


def parse_psi_all(text: str) -> dict[str, float]:
    """``some_avg10``/``some_avg60``/``full_avg10``/``full_avg60`` from a /proc/pressure file."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        kind, _, rest = line.strip().partition(" ")
        if kind not in ("some", "full"):
            continue
        for field in rest.split():
            key, _, val = field.partition("=")
            if key in ("avg10", "avg60"):
                try:
                    out[f"{kind}_{key}"] = float(val)
                except ValueError:
                    pass
    return out


def parse_pid_stat(text: str) -> tuple[str, int, int] | None:
    """``(comm, utime + stime ticks, rss pages)`` from /proc/<pid>/stat."""
    head, sep, rest = text.rpartition(")")
    if not sep:
        return None
    comm = head.partition("(")[2]
    f = rest.split()
    try:  # fields after "comm": state is f[0]; utime/stime are 14/15, rss 24 (1-based)
        return comm, int(f[11]) + int(f[12]), int(f[21])
    except (IndexError, ValueError):
        return None


def parse_net_dev(text: str) -> dict[str, tuple[int, int]]:
    """``{iface: (rx bytes, tx bytes)}`` from /proc/net/dev."""
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines()[2:]:
        name, sep, rest = line.partition(":")
        f = rest.split()
        if sep and len(f) >= 9:
            try:
                out[name.strip()] = (int(f[0]), int(f[8]))
            except ValueError:
                continue
    return out


def parse_default_route(text: str) -> tuple[str | None, str | None]:
    """``(gateway, dev)`` of the first ``default via ... dev ...`` line of ``ip route``."""
    for line in text.splitlines():
        f = line.split()
        if f[:1] == ["default"]:
            gw = f[f.index("via") + 1] if "via" in f and f.index("via") + 1 < len(f) else None
            dev = f[f.index("dev") + 1] if "dev" in f and f.index("dev") + 1 < len(f) else None
            return gw, dev
    return None, None


_NUM = r"(-?\d+(?:\.\d+)?)"
_STATION_FIELDS = {
    "signal_dbm": re.compile(rf"^\s*signal:\s*{_NUM}", re.M),
    "signal_avg_dbm": re.compile(rf"^\s*signal avg:\s*{_NUM}", re.M),
    "tx_mbit": re.compile(rf"^\s*tx bitrate:\s*{_NUM}", re.M),
    "rx_mbit": re.compile(rf"^\s*rx bitrate:\s*{_NUM}", re.M),
    "tx_retries": re.compile(r"^\s*tx retries:\s*(\d+)", re.M),
    "tx_failed": re.compile(r"^\s*tx failed:\s*(\d+)", re.M),
    "beacon_loss": re.compile(r"^\s*beacon loss:\s*(\d+)", re.M),
    "expected_mbit": re.compile(rf"^\s*expected throughput:\s*{_NUM}", re.M),
}
_FREQ_RE = re.compile(rf"^\s*freq:\s*{_NUM}", re.M)


def parse_iw_station(text: str) -> dict[str, float]:
    """Numbers from ``iw dev <if> station dump`` (the first station: the AP)."""
    first = text.split("\nStation ", 1)[0]
    out: dict[str, float] = {}
    for key, rx in _STATION_FIELDS.items():
        m = rx.search(first)
        if m:
            out[key] = float(m.group(1))
    return out


_SS_NUMS = {
    "rtt_ms": re.compile(r"\brtt:([\d.]+)/"),
    "rttvar_ms": re.compile(r"\brtt:[\d.]+/([\d.]+)"),
    "minrtt_ms": re.compile(r"\bminrtt:([\d.]+)"),
    "cwnd": re.compile(r"\bcwnd:(\d+)"),
    "bytes_sent": re.compile(r"\bbytes_sent:(\d+)"),
    "bytes_retrans": re.compile(r"\bbytes_retrans:(\d+)"),
    "retrans_total": re.compile(r"\bretrans:\d+/(\d+)"),
    "unacked": re.compile(r"\bunacked:(\d+)"),
    "notsent": re.compile(r"\bnotsent:(\d+)"),
    "lost": re.compile(r"\blost:(\d+)"),
}
_SS_RATES = {
    "delivery_bps": re.compile(r"\bdelivery_rate (\d+)bps"),
    "send_bps": re.compile(r"\bsend (\d+)bps"),
    "pacing_bps": re.compile(r"\bpacing_rate (\d+)bps"),
}


def parse_ss_obs(text: str) -> dict[str, Any] | None:
    """OBS's RTMP socket from ``ss -tinpH state established``: the busiest remote one.

    ``ss`` prints a socket line (with ``users:(("obs",...))``) and an info line.
    Loopback peers (obs-websocket clients) are skipped.
    """
    lines = text.splitlines()
    best: dict[str, Any] | None = None
    for i, line in enumerate(lines):
        if '(("obs"' not in line:
            continue
        cols = line.split()
        peer = cols[3] if len(cols) > 3 else ""
        if peer.startswith(("127.", "[::1]", "[::ffff:127.")):
            continue
        info = lines[i + 1] if i + 1 < len(lines) and not lines[i + 1][:1].isdigit() else ""
        rec: dict[str, Any] = {"peer": peer}
        for key, rx in _SS_NUMS.items():
            m = rx.search(info)
            if m:
                rec[key] = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
        for key, rx in _SS_RATES.items():
            m = rx.search(info)
            if m:
                rec[key] = int(m.group(1))
        if best is None or rec.get("bytes_sent", 0) > best.get("bytes_sent", 0):
            best = rec
    return best


_PING_RE = re.compile(r"time[=<]([\d.]+) ms")


def parse_ping(text: str) -> float | None:
    m = _PING_RE.search(text)
    return float(m.group(1)) if m else None


def process_group(comm: str, argv: list[str]) -> str | None:
    """The group a process is reported under (None: not reported)."""
    if comm.startswith(("chromium", "chrome")):
        return "chromium-renderer" if _chromium_label(argv)[0] == "chromium-renderer" else "chromium-other"
    if comm == "obs":
        return "obs"
    if comm in ("kwin_wayland", "plasmashell", "Xwayland", "ffmpeg"):
        return comm
    if comm.startswith(("pipewire", "wireplumber")):
        return "pipewire"
    for i, arg in enumerate(argv[:2]):
        if arg.endswith("/stream-pilot") or arg == "stream-pilot":
            sub = argv[i + 1] if i + 1 < len(argv) and argv[i + 1] else ""
            return f"stream-pilot-{sub}" if sub and not sub.startswith("-") else "stream-pilot"
    if comm.startswith("stream-pilot"):
        return "stream-pilot"
    return None


def scrub(text: str) -> str:
    return _KEY_RE.sub("FB-<redacted>", text)


# ------------------------------------------------------------- collector


@dataclass
class _Prev:
    t: float = 0.0
    cpu: tuple[int, int] | None = None
    procs: dict[int, tuple[str, int]] | None = None
    net: dict[str, tuple[int, int]] | None = None


class Collector:
    """Samples into ``<stream dir>/diag/`` until :meth:`stop`; then exports and summarizes."""

    def __init__(
        self,
        stream_dir: Path,
        cfg: Config,
        *,
        obs: Any = None,
        proc_root: Path = Path("/proc"),
        sys_root: Path = Path("/sys"),
        clock: Callable[[], float] = time.time,
        run: Callable[..., Any] | None = None,
    ):
        self.stream_dir = Path(stream_dir)
        self.dir = self.stream_dir / DIAG_DIR
        self.cfg = cfg
        self.d: DiagnosticsCfg = cfg.diagnostics
        self.obs = obs
        self.proc = proc_root
        self.sys = sys_root
        self.clock = clock
        self._run = run or _run_cmd
        self.started_at = clock()
        self._stop = asyncio.Event()
        self._files: dict[str, Any] = {}
        self._errors: dict[str, int] = {}
        self._sys_prev = _Prev()
        self._net_prev = _Prev()
        self._clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self._page_kb = (os.sysconf("SC_PAGE_SIZE") // 1024) if hasattr(os, "sysconf") else 4

    # -- output ------------------------------------------------------------

    def _write(self, name: str, rec: dict) -> None:
        f = self._files.get(name)
        if f is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            f = self._files[name] = open(self.dir / name, "a", buffering=1, encoding="utf-8")
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def _failed(self, what: str, exc: BaseException) -> None:
        n = self._errors[what] = self._errors.get(what, 0) + 1
        if n == 1 or n % 100 == 0:
            log.warning("diagnostics: %s failed (%d times): %r", what, n, exc)

    def stop(self) -> None:
        self._stop.set()

    # -- loop --------------------------------------------------------------

    async def run(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._write("events.jsonl", {"t": round(self.clock(), 3), "event": "collector_started"})
        loops = [
            self._every(self.d.obs_s, "obs", self.sample_obs),
            self._every(self.d.network_s, "network", self.sample_network),
            self._every(self.d.system_s, "system", self.sample_system),
        ]
        if self.d.gateway_status_url:
            loops.append(self._every(self.d.gateway_s, "gateway", self.sample_gateway))
        tasks = [asyncio.create_task(c) for c in loops]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._write("events.jsonl", {"t": round(self.clock(), 3), "event": "collector_stopped",
                                         "errors": self._errors})
            await self.finish()

    async def _every(self, period: float, what: str, fn: Callable[[], Any]) -> None:
        period = max(0.5, float(period))
        while True:
            started = time.monotonic()
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failed(what, exc)
            await asyncio.sleep(max(0.1, period - (time.monotonic() - started)))

    # -- samplers ------------------------------------------------------------

    async def sample_obs(self) -> None:
        if self.obs is None:
            return
        stats = await self.obs.request("GetStats") or {}
        stream = await self.obs.request("GetStreamStatus") or {}
        scene = await self.obs.request("GetCurrentProgramScene") or {}
        rec = {"t": round(self.clock(), 3)}
        for key in ("activeFps", "averageFrameRenderTime", "cpuUsage", "memoryUsage",
                    "renderSkippedFrames", "renderTotalFrames", "outputSkippedFrames",
                    "outputTotalFrames"):
            rec[key] = stats.get(key)
        for key in ("outputActive", "outputReconnecting", "outputSkippedFrames",
                    "outputTotalFrames", "outputBytes", "outputCongestion", "outputDuration"):
            rec["net_" + key] = stream.get(key)
        rec["scene"] = scene.get("currentProgramSceneName", scene.get("sceneName"))
        self._write("obs.jsonl", rec)

    async def sample_network(self) -> None:
        now = self.clock()
        rec: dict[str, Any] = {"t": round(now, 3)}
        gw, dev = parse_default_route(await self._run(["ip", "route", "show", "default"]))
        rec["dev"] = dev
        net = parse_net_dev(_read(self.proc / "net/dev"))
        prev = self._net_prev
        if dev and dev in net and prev.net and dev in prev.net and now > prev.t:
            dt = now - prev.t
            rec["rx_kbps"] = round((net[dev][0] - prev.net[dev][0]) * 8 / dt / 1000, 1)
            rec["tx_kbps"] = round((net[dev][1] - prev.net[dev][1]) * 8 / dt / 1000, 1)
        self._net_prev = _Prev(t=now, net=net)
        if dev and (self.sys / "class/net" / dev / "wireless").exists():
            rec["wifi"] = parse_iw_station(await self._run(["iw", "dev", dev, "station", "dump"]))
            link = await self._run(["iw", "dev", dev, "link"])
            m = _FREQ_RE.search(link)
            if m:
                rec["wifi"]["freq_mhz"] = float(m.group(1))
        rec["rtmp"] = parse_ss_obs(await self._run(["ss", "-tinpH", "state", "established"]))
        if gw:
            rec["gw_ping_ms"] = parse_ping(await self._run(["ping", "-n", "-c", "1", "-W", "1", gw]))
        self._write("network.jsonl", rec)

    async def sample_system(self) -> None:
        now = self.clock()
        rec: dict[str, Any] = {"t": round(now, 3)}
        cpu = parse_proc_stat_cpu(_read(self.proc / "stat"))
        prev = self._sys_prev
        if cpu and prev.cpu and cpu[1] > prev.cpu[1]:
            rec["cpu_pct"] = round(100.0 * (cpu[0] - prev.cpu[0]) / (cpu[1] - prev.cpu[1]), 1)
        rec["load1"] = _float(_read(self.proc / "loadavg").split()[:1])
        mem = parse_meminfo(_read(self.proc / "meminfo"))
        rec["mem_available_mb"] = mem.get("MemAvailable", 0) // 1024
        rec["swap_used_mb"] = (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) // 1024
        for kind in ("cpu", "memory", "io"):
            try:
                rec[f"psi_{kind}"] = parse_psi_all(_read(self.proc / "pressure" / kind))
            except OSError:
                pass
        procs = self._scan_procs()
        groups: dict[str, dict[str, float]] = {}
        dt = now - prev.t if prev.t else 0.0
        for pid, (group, ticks, rss_kb) in procs.items():
            g = groups.setdefault(group, {"n": 0, "rss_mb": 0.0, "cpu_pct": 0.0})
            g["n"] += 1
            g["rss_mb"] += rss_kb / 1024
            old = (prev.procs or {}).get(pid)
            if dt > 0 and old is not None and old[0] == group and ticks >= old[1]:
                g["cpu_pct"] += 100.0 * (ticks - old[1]) / self._clk_tck / dt
        for g in groups.values():
            g["rss_mb"] = round(g["rss_mb"], 1)
            g["cpu_pct"] = round(g["cpu_pct"], 1)
        rec["procs"] = groups
        rec["temps_c"] = self._temps()
        rec["cpu_mhz"] = self._cpu_mhz()
        rec["gpu_mhz"] = self._gpu_mhz()
        self._sys_prev = _Prev(t=now, cpu=cpu, procs={p: (g, t) for p, (g, t, _) in procs.items()})
        self._write("system.jsonl", rec)

    async def sample_gateway(self) -> None:
        url = self.d.gateway_status_url

        def fetch() -> bytes:
            req = urllib.request.Request(url, headers={"User-Agent": "stream-pilot"})
            with urllib.request.urlopen(req, timeout=GATEWAY_TIMEOUT_S) as resp:
                return resp.read(GATEWAY_MAX_BYTES)

        body = await asyncio.to_thread(fetch)
        data = json.loads(body)
        if isinstance(data, dict) and isinstance(data.get("signal"), dict):
            data = {"signal": data["signal"]}
        self._write("gateway.jsonl", {"t": round(self.clock(), 3), "status": data})

    # -- /proc and /sys -------------------------------------------------------

    def _scan_procs(self) -> dict[int, tuple[str, int, int]]:
        out: dict[int, tuple[str, int, int]] = {}
        try:
            entries = [e for e in self.proc.iterdir() if e.name.isdigit()]
        except OSError:
            return out
        for entry in entries:
            try:
                parsed = parse_pid_stat((entry / "stat").read_text(errors="replace"))
                if parsed is None:
                    continue
                comm, ticks, rss_pages = parsed
                argv: list[str] = []
                if comm.startswith(("chromium", "chrome", "python", "stream-pilot")):
                    argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
                group = process_group(comm, argv)
                if group is not None:
                    out[int(entry.name)] = (group, ticks, rss_pages * self._page_kb)
            except OSError:
                continue
        return out

    def _temps(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for hw in sorted((self.sys / "class/hwmon").glob("hwmon*")):
            name = _read(hw / "name").strip() or hw.name
            vals = []
            for t in hw.glob("temp*_input"):
                try:
                    vals.append(int(_read(t).strip()) / 1000.0)
                except ValueError:
                    continue
            if vals:
                out[name] = max(vals)
        return out

    def _cpu_mhz(self) -> float | None:
        vals = []
        for f in (self.sys / "devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"):
            try:
                vals.append(int(_read(f).strip()) / 1000.0)
            except ValueError:
                continue
        return round(statistics.fmean(vals)) if vals else None

    def _gpu_mhz(self) -> float | None:
        for f in sorted((self.sys / "class/drm").glob("card*/gt_act_freq_mhz")):
            try:
                return float(_read(f).strip())
            except ValueError:
                continue
        return None

    # -- the end ---------------------------------------------------------------

    async def finish(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()
        since = f"@{int(self.started_at - JOURNAL_BACKLOG_S)}"
        exports = {
            "journal-user.txt": ["journalctl", "--user", "--since", since, "-o", "short-unix", "--no-pager"],
            "journal-system.txt": ["journalctl", "--since", since, "-o", "short-unix", "--no-pager",
                                   "-k", "-u", "NetworkManager", "-u", "wpa_supplicant",
                                   "-u", "systemd-logind", "-u", "greenboot-healthcheck"],
        }
        for name, argv in exports.items():
            try:
                text = await self._run(argv, timeout=60.0)
                (self.dir / name).write_text(scrub(text), encoding="utf-8")
            except Exception as exc:
                self._failed(f"export {name}", exc)
        logs = Path("~/.config/obs-studio/logs").expanduser()
        try:
            for f in sorted(logs.glob("*.txt")):
                if f.stat().st_mtime >= self.started_at - JOURNAL_BACKLOG_S:
                    (self.dir / f"obs-{f.name.replace(' ', '_')}").write_text(
                        scrub(f.read_text(errors="replace")), encoding="utf-8"
                    )
        except OSError as exc:
            self._failed("export obs logs", exc)
        try:
            (self.dir / "summary.txt").write_text(summarize(self.stream_dir), encoding="utf-8")
        except Exception as exc:
            self._failed("summary", exc)
            log.exception("diagnostics: summary failed")


async def _run_cmd(argv: list[str], timeout: float = SUBPROCESS_TIMEOUT_S) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    return out.decode(errors="replace")


def _read(path: Path) -> str:
    return Path(path).read_text(errors="replace")


def _float(parts: list[str]) -> float | None:
    try:
        return float(parts[0])
    except (IndexError, ValueError):
        return None


def run_collector(stream_dir: Path, cfg: Config) -> int:
    """``stream-pilot diag``: collect until SIGTERM/SIGINT, then export and summarize."""
    from .obs import SimpleObsClient

    async def main() -> None:
        collector = Collector(stream_dir, cfg, obs=SimpleObsClient(cfg.obs))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, collector.stop)
        await collector.run()

    asyncio.run(main())
    return 0


# ---------------------------------------------------------------- summary


def _jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _pct(vals: list[float], p: float) -> float | None:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _fmt(v: Any, nd: int = 1) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _hms(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def _stats_line(name: str, vals: Iterable[float | None], unit: str = "", nd: int = 1) -> str:
    v = [x for x in vals if isinstance(x, (int, float))]
    if not v:
        return f"{name}: -"
    return (f"{name}: min {_fmt(float(min(v)), nd)}  p50 {_fmt(_pct(v, 50), nd)}  "
            f"p95 {_fmt(_pct(v, 95), nd)}  max {_fmt(float(max(v)), nd)}{unit}")


def _counter_bursts(rows: list[dict], key: str) -> tuple[int, list[tuple[float, int]]]:
    """Total increase of a counter (resets, e.g. after a reconnect, are skipped) and its bursts."""
    total, bursts, prev = 0, [], None
    for r in rows:
        v = r.get(key)
        if not isinstance(v, (int, float)):
            continue
        if prev is not None and v >= prev and v - prev > 0:
            total += int(v - prev)
            bursts.append((r["t"], int(v - prev)))
        prev = v
    return total, bursts


def summarize(stream_dir: Path) -> str:
    """A plain-text review of ``<stream dir>/diag`` and the stream's comments."""
    d = Path(stream_dir) / DIAG_DIR
    obs, net, sysr = _jsonl(d / "obs.jsonl"), _jsonl(d / "network.jsonl"), _jsonl(d / "system.jsonl")
    out: list[str] = [f"diagnostics summary: {Path(stream_dir).name}"]
    if obs:
        t0, t1 = obs[0]["t"], obs[-1]["t"]
        out.append(f"window: {_hms(t0)} - {_hms(t1)} ({(t1 - t0) / 60:.1f} min, {len(obs)} OBS samples)")

    out.append("\n== OBS")
    for key, label in (("renderSkippedFrames", "skipped (rendering lag)"),
                       ("outputSkippedFrames", "skipped (encoding lag)"),
                       ("net_outputSkippedFrames", "dropped (network)")):
        total, bursts = _counter_bursts(obs, key)
        line = f"{label}: {total} frames in {len(bursts)} sample(s)"
        if bursts:
            worst = sorted(bursts, key=lambda b: -b[1])[:8]
            line += "; largest: " + ", ".join(f"{_hms(t)} +{n}" for t, n in sorted(worst))
        out.append(line)
    out.append(_stats_line("render time", [r.get("averageFrameRenderTime") for r in obs], " ms", 2))
    out.append(_stats_line("OBS CPU", [r.get("cpuUsage") for r in obs], " %"))
    out.append(_stats_line("congestion", [r.get("net_outputCongestion") for r in obs], "", 2))
    rates = []
    for a, b in zip(obs, obs[1:]):
        x, y = a.get("net_outputBytes"), b.get("net_outputBytes")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and y >= x and b["t"] > a["t"]:
            rates.append((y - x) * 8 / (b["t"] - a["t"]) / 1000)
    out.append(_stats_line("output bitrate", rates, " kbps", 0))
    recon = [r["t"] for r in obs if r.get("net_outputReconnecting")]
    inactive = [r["t"] for r in obs if r.get("net_outputActive") is False]
    out.append(f"reconnecting: {len(recon)} sample(s){' from ' + _hms(recon[0]) if recon else ''}; "
               f"output inactive: {len(inactive)} sample(s)")
    scenes: dict[str, int] = {}
    for r in obs:
        scenes[str(r.get("scene"))] = scenes.get(str(r.get("scene")), 0) + 1
    out.append("program scenes (samples): " + ", ".join(f"{k} {v}" for k, v in scenes.items()))

    out.append("\n== network")
    rt = [r.get("rtmp") or {} for r in net]
    out.append(_stats_line("RTMP RTT", [x.get("rtt_ms") for x in rt], " ms"))
    retr = [x.get("retrans_total") for x in rt if isinstance(x.get("retrans_total"), int)]
    sent = [x.get("bytes_sent") for x in rt if isinstance(x.get("bytes_sent"), int)]
    if retr and sent:
        out.append(f"RTMP retransmits: {max(retr) - min(retr)} segment(s); sent {(max(sent) - min(sent)) / 1e6:.0f} MB")
    out.append(_stats_line("RTMP unsent bytes", [x.get("notsent") for x in rt], "", 0))
    out.append(_stats_line("RTMP delivery rate", [(x.get("delivery_bps") or 0) / 1000 or None for x in rt], " kbps", 0))
    out.append(_stats_line("uplink", [r.get("tx_kbps") for r in net], " kbps", 0))
    pings = [r.get("gw_ping_ms") for r in net if "gw_ping_ms" in r]
    lost = sum(1 for p in pings if p is None)
    out.append(_stats_line("gateway ping", pings, " ms") + f"  (lost {lost}/{len(pings)})")
    wifi = [r.get("wifi") or {} for r in net if r.get("wifi")]
    if wifi:
        out.append(_stats_line("Wi-Fi signal", [w.get("signal_dbm") for w in wifi], " dBm", 0))
        out.append(_stats_line("Wi-Fi tx bitrate", [w.get("tx_mbit") for w in wifi], " Mbit/s", 0))
        tr = [w.get("tx_retries") for w in wifi if w.get("tx_retries") is not None]
        tf = [w.get("tx_failed") for w in wifi if w.get("tx_failed") is not None]
        bl = [w.get("beacon_loss") for w in wifi if w.get("beacon_loss") is not None]
        if tr:
            out.append(f"Wi-Fi tx retries +{int(max(tr) - min(tr))}, tx failed +{int(max(tf) - min(tf)) if tf else '-'}, "
                       f"beacon loss +{int(max(bl) - min(bl)) if bl else '-'}")

    out.append("\n== system")
    out.append(_stats_line("CPU busy", [r.get("cpu_pct") for r in sysr], " %"))
    out.append(_stats_line("memory available", [r.get("mem_available_mb") for r in sysr], " MB", 0))
    out.append(_stats_line("CPU pressure some avg10", [(r.get("psi_cpu") or {}).get("some_avg10") for r in sysr], " %"))
    out.append(_stats_line("I/O pressure some avg10", [(r.get("psi_io") or {}).get("some_avg10") for r in sysr], " %"))
    out.append(_stats_line("GPU clock", [r.get("gpu_mhz") for r in sysr], " MHz", 0))
    groups = sorted({g for r in sysr for g in (r.get("procs") or {})})
    for g in groups:
        cpu = [((r.get("procs") or {}).get(g) or {}).get("cpu_pct") for r in sysr]
        rss = [((r.get("procs") or {}).get(g) or {}).get("rss_mb") for r in sysr]
        rss_v = [(r["t"], x) for r, x in zip(sysr, rss) if isinstance(x, (int, float))]
        growth = ""
        if len(rss_v) >= 2 and rss_v[-1][0] > rss_v[0][0]:
            per_h = (rss_v[-1][1] - rss_v[0][1]) / ((rss_v[-1][0] - rss_v[0][0]) / 3600)
            growth = f", RSS {rss_v[0][1]:.0f} -> {rss_v[-1][1]:.0f} MB ({per_h:+.0f} MB/h)"
        cpu_v = [x for x in cpu if isinstance(x, (int, float))]
        out.append(f"{g}: CPU p50 {_fmt(_pct(cpu_v, 50))} max {_fmt(max(cpu_v) if cpu_v else None)} %{growth}")
    temps = sorted({k for r in sysr for k in (r.get("temps_c") or {})})
    for k in temps:
        out.append(_stats_line(f"temp {k}", [(r.get("temps_c") or {}).get(k) for r in sysr], " C", 0))

    out.append("\n== comments")
    comments = _jsonl(Path(stream_dir) / "comments.jsonl")
    by_src: dict[str, int] = {}
    for c in comments:
        by_src[str(c.get("source"))] = by_src.get(str(c.get("source")), 0) + 1
    out.append("lines by source: " + (", ".join(f"{k} {v}" for k, v in sorted(by_src.items())) or "none"))
    lags = []
    for c in comments:
        if c.get("source") != "fb_dom" or not c.get("created_at") or not c.get("captured_at"):
            continue
        try:
            created = datetime.fromisoformat(str(c["created_at"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        lags.append(float(c["captured_at"]) - created)
    out.append(_stats_line("capture lag (Facebook time to captured)", lags, " s"))
    per_min: dict[int, int] = {}
    for c in comments:
        if c.get("source") == "fb_dom" and isinstance(c.get("captured_at"), (int, float)):
            per_min[int(c["captured_at"] // 60)] = per_min.get(int(c["captured_at"] // 60), 0) + 1
    if per_min:
        out.append(f"fb_dom comments per minute: max {max(per_min.values())}, "
                   f"mean {statistics.fmean(per_min.values()):.1f} over {len(per_min)} active minute(s)")

    out.append("\n== events (controller journal)")
    journal = _read_text(d / "journal-user.txt")
    for label, rx in (
        ("state changes", r"state -> "),
        ("comment column reloads (stale)", r"comments column missed"),
        ("comment column checks: up to date", r"comments column is up to date"),
        ("capture re-attaches", r"re-attaching to live"),
        ("deck warnings", r"deck warning: (?!none)"),
        ("display mirror log lines", r"stream_pilot\.display"),
        ("warnings", r"\bWARNING\b"),
        ("errors", r"\bERROR\b|Traceback"),
    ):
        out.append(f"{label}: {len(re.findall(rx, journal))}")
    errors = _jsonl(d / "events.jsonl")
    stopped = [e for e in errors if e.get("event") == "collector_stopped"]
    if stopped and stopped[-1].get("errors"):
        out.append(f"collector sampling errors: {stopped[-1]['errors']}")
    return "\n".join(out) + "\n"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""
