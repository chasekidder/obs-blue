"""Tests for memory.py. Never reads the host's /proc: fixture text + fake trees."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from stream_pilot.config import MemoryCfg
from stream_pilot.memory import (
    MemoryGuard,
    ProcSample,
    evaluate,
    parse_meminfo,
    parse_psi,
    sample_processes,
    take_sample,
)
from stream_pilot.states import KeyFace

DATA = Path(__file__).parent / "data"
MEM_FACE = KeyFace("MEM", "#e67e00", blink=True)
BLANK = KeyFace("", "#000000")
CFG = MemoryCfg(interval_s=60, floor_mb=600, popout_rss_warn_mb=1500, auto_reload_popout=False)


# --------------------------------------------------------------------- helpers


def add_proc(root: Path, pid: int, comm: str, rss_kb: int | None, cmdline: list[str] | None = None):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "comm").write_text(comm + "\n")
    argv = cmdline if cmdline is not None else [comm]
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    status = f"Name:\t{comm}\nState:\tS (sleeping)\nPid:\t{pid}\n"
    if rss_kb is not None:
        status += f"VmRSS:\t{rss_kb:8d} kB\n"
    status += "Threads:\t1\n"
    (d / "status").write_text(status)
    return d


def make_proc_tree(root: Path, *, psi: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "meminfo").write_text((DATA / "meminfo.txt").read_text())
    if psi:
        (root / "pressure").mkdir()
        (root / "pressure" / "memory").write_text((DATA / "pressure_memory.txt").read_text())
    add_proc(root, 1, "systemd", 12000)  # unrelated, skipped
    add_proc(root, 100, "obs", 1_500_000, ["/usr/bin/obs"])
    add_proc(root, 101, "kwin_wayland", 300_000)
    add_proc(root, 102, "plasmashell", 450_000)
    add_proc(root, 200, "chromium", 400_000, ["/usr/lib/chromium/chromium", "--ozone-platform=wayland"])
    add_proc(
        root, 201, "chromium", 1_800_000,
        ["/usr/lib/chromium/chromium", "--type=renderer", "--renderer-client-id=7", "--lang=en-US"],
    )
    add_proc(root, 202, "chromium", 250_000, ["/usr/lib/chromium/chromium", "--type=gpu-process"])
    add_proc(root, 2, "kthreadd", None, [])  # kernel thread: no VmRSS, empty cmdline
    (root / "self").mkdir()  # non-numeric dir, ignored
    (root / "300").mkdir()  # vanished mid-read: no files at all
    return root


# --------------------------------------------------------------------- parsers


def test_parse_meminfo_fixture():
    info = parse_meminfo((DATA / "meminfo.txt").read_text())
    assert info["MemTotal"] == 7935008
    assert info["MemAvailable"] == 2097152
    assert info["Active(anon)"] == 2301840
    assert info["HugePages_Total"] == 0  # unitless line


def test_parse_meminfo_ignores_garbage_lines():
    assert parse_meminfo("bogus\nMemFree: abc kB\nMemAvailable: 5 kB\n\n") == {"MemAvailable": 5}


def test_parse_psi_fixture():
    psi = parse_psi((DATA / "pressure_memory.txt").read_text())
    assert psi == {"some_avg60": 1.23, "full_avg60": 0.40}


def test_parse_psi_missing_full_line_is_zero():
    assert parse_psi("some avg10=0.00 avg60=2.50 avg300=0.50 total=1\n") == {
        "some_avg60": 2.5,
        "full_avg60": 0.0,
    }


# ----------------------------------------------------------- sample_processes


def test_sample_processes_fake_tree(tmp_path):
    root = make_proc_tree(tmp_path / "proc")
    procs = sample_processes(root)
    assert all(isinstance(p, ProcSample) for p in procs)
    by_pid = {p.pid: p for p in procs}
    assert sorted(by_pid) == [100, 101, 102, 200, 201, 202]  # sorted by pid too
    assert [p.pid for p in procs] == sorted(by_pid)
    assert by_pid[100].label == "obs" and by_pid[100].rss_kb == 1_500_000
    assert by_pid[101].label == "kwin_wayland"
    assert by_pid[102].label == "plasmashell"
    assert by_pid[200].label == "chromium-browser"
    assert by_pid[201].label == "chromium-renderer"
    assert by_pid[201].client_id == "7"
    assert by_pid[202].label == "chromium-gpu-process"
    assert by_pid[200].client_id is None


def test_sample_processes_chrome_comm_and_other_types(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    add_proc(root, 10, "chrome", 1000, ["chrome", "--type=utility"])
    assert [p.label for p in sample_processes(root)] == ["chromium-utility"]


def test_sample_processes_unreadable_cmdline_falls_back_to_browser(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    d = add_proc(root, 10, "chromium", 1000)
    (d / "cmdline").unlink()
    assert [p.label for p in sample_processes(root)] == ["chromium-browser"]


def test_sample_processes_permission_error_skipped(tmp_path, monkeypatch):
    root = make_proc_tree(tmp_path / "proc")
    real = Path.read_text

    def fake_read(self, *a, **kw):
        if self.parent.name == "101" and self.name == "status":
            raise PermissionError("nope")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", fake_read)
    assert 101 not in {p.pid for p in sample_processes(root)}


def test_sample_processes_missing_root(tmp_path):
    assert sample_processes(tmp_path / "nope") == []


# ----------------------------------------------------------------- take_sample


def test_take_sample_shape(tmp_path):
    root = make_proc_tree(tmp_path / "proc")
    s = take_sample(root)
    assert set(s) == {"mem_available_mb", "psi", "procs"}
    assert s["mem_available_mb"] == 2048
    assert s["psi"] == {"some_avg60": 1.23, "full_avg60": 0.40}
    renderer = next(p for p in s["procs"] if p["pid"] == 201)
    assert renderer == {
        "pid": 201,
        "comm": "chromium",
        "rss_kb": 1_800_000,
        "rss_mb": 1_800_000 // 1024,
        "label": "chromium-renderer",
        "client_id": "7",
    }
    obs = next(p for p in s["procs"] if p["pid"] == 100)
    assert "client_id" not in obs
    json.dumps(s)  # JSON-serialisable


def test_take_sample_without_psi(tmp_path):
    root = make_proc_tree(tmp_path / "proc", psi=False)
    s = take_sample(root)
    assert s["psi"] is None
    assert s["mem_available_mb"] == 2048


def test_take_sample_missing_meminfo_raises(tmp_path):
    (tmp_path / "proc").mkdir()
    with pytest.raises(OSError):
        take_sample(tmp_path / "proc")


# -------------------------------------------------------------------- evaluate


def sample(avail_mb=2000, procs=()):
    return {"mem_available_mb": avail_mb, "psi": None, "procs": list(procs)}


def proc(label, rss_mb, pid=1):
    return {"pid": pid, "comm": "x", "rss_kb": rss_mb * 1024, "rss_mb": rss_mb, "label": label}


def test_evaluate_ok():
    assert evaluate(sample(), CFG) is None


def test_evaluate_floor_is_strict_less_than():
    assert evaluate(sample(avail_mb=600), CFG) is None
    assert evaluate(sample(avail_mb=599), CFG) == "MEM"


def test_evaluate_renderer_rss_threshold():
    assert evaluate(sample(procs=[proc("chromium-renderer", 1500)]), CFG) is None
    assert evaluate(sample(procs=[proc("chromium-renderer", 1501)]), CFG) == "MEM"


def test_evaluate_any_renderer_triggers():
    procs = [proc("chromium-renderer", 300, 1), proc("chromium-renderer", 2000, 2)]
    assert evaluate(sample(procs=procs), CFG) == "MEM"


def test_evaluate_ignores_non_renderer_chromium_and_others():
    procs = [
        proc("chromium-browser", 4000),
        proc("chromium-gpu-process", 4000),
        proc("obs", 4000),
    ]
    assert evaluate(sample(procs=procs), CFG) is None


# ----------------------------------------------------------------- MemoryGuard


class FakeDeck:
    def __init__(self):
        self.shown: list[tuple[int, KeyFace]] = []

    def show(self, key, face):
        self.shown.append((key, face))

    def set_event_sink(self, sink): ...

    async def start(self): ...


class Harness:
    """Sampler returns scripted samples (last repeats); sleep cancels after N calls."""

    def __init__(self, samples, sleeps=3):
        self.samples = list(samples)
        self.i = 0
        self.sleeps = sleeps
        self.sleep_args: list[float] = []

    def sampler(self):
        item = self.samples[min(self.i, len(self.samples) - 1)]
        self.i += 1
        if isinstance(item, Exception):
            raise item
        return item

    async def sleep(self, s):
        self.sleep_args.append(s)
        if len(self.sleep_args) >= self.sleeps:
            raise asyncio.CancelledError

    async def run(self, guard):
        with pytest.raises(asyncio.CancelledError):
            await guard.run()


def make_guard(h, deck, got, cfg=CFG, on_sample=None):
    return MemoryGuard(cfg, deck, on_sample or got.append, sampler=h.sampler, sleep=h.sleep)


async def test_guard_one_iteration_logs_json_and_notifies(caplog):
    s = sample(procs=[proc("obs", 1400)])
    h, deck, got = Harness([s], sleeps=1), FakeDeck(), []
    with caplog.at_level(logging.INFO, logger="stream_pilot.memory"):
        await h.run(make_guard(h, deck, got))
    assert got == [s]
    assert h.sleep_args == [60]
    lines = [r for r in caplog.records if r.name == "stream_pilot.memory"]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert json.loads(lines[0].getMessage()) == s
    assert deck.shown == [(4, BLANK)]


async def test_guard_shows_mem_and_only_on_change():
    ok, low = sample(), sample(avail_mb=100)
    # ok, ok, low, low, ok
    h = Harness([ok, ok, low, low, ok], sleeps=5)
    deck, got = FakeDeck(), []
    await h.run(make_guard(h, deck, got))
    assert len(got) == 5
    assert deck.shown == [(4, BLANK), (4, MEM_FACE), (4, BLANK)]


async def test_guard_first_iteration_shows_mem_when_low():
    h = Harness([sample(avail_mb=1)], sleeps=2)
    deck, got = FakeDeck(), []
    await h.run(make_guard(h, deck, got))
    assert deck.shown == [(4, MEM_FACE)]


async def test_guard_sampler_error_logged_and_loop_continues(caplog):
    h = Harness([OSError("boom"), sample(avail_mb=1)], sleeps=2)
    deck, got = FakeDeck(), []
    with caplog.at_level(logging.INFO, logger="stream_pilot.memory"):
        await h.run(make_guard(h, deck, got))
    assert len(got) == 1  # second iteration only
    assert deck.shown == [(4, MEM_FACE)]
    assert any(r.levelno >= logging.WARNING and "boom" in r.getMessage() for r in caplog.records)


async def test_guard_on_sample_error_does_not_stop_loop_or_deck(caplog):
    def bad(_s):
        raise RuntimeError("controller broke")

    h = Harness([sample(avail_mb=1)], sleeps=2)
    deck = FakeDeck()
    with caplog.at_level(logging.INFO, logger="stream_pilot.memory"):
        await h.run(make_guard(h, deck, [], on_sample=bad))
    assert h.i == 2
    assert deck.shown == [(4, MEM_FACE)]
    assert any("controller broke" in r.getMessage() for r in caplog.records)


async def test_guard_deck_error_retried_next_iteration(caplog):
    class FlakyDeck(FakeDeck):
        def show(self, key, face):
            if not self.shown and not getattr(self, "failed", False):
                self.failed = True
                raise RuntimeError("usb gone")
            super().show(key, face)

    h = Harness([sample()], sleeps=3)
    deck = FlakyDeck()
    with caplog.at_level(logging.INFO, logger="stream_pilot.memory"):
        await h.run(make_guard(h, deck, []))
    assert deck.shown == [(4, BLANK)]  # shown once after the failed first attempt
    assert any("usb gone" in r.getMessage() for r in caplog.records)


async def test_guard_uses_configured_interval():
    cfg = MemoryCfg(interval_s=5, floor_mb=600, popout_rss_warn_mb=1500, auto_reload_popout=True)
    h = Harness([sample()], sleeps=2)
    await h.run(make_guard(h, FakeDeck(), [], cfg=cfg))
    assert h.sleep_args == [5, 5]


async def test_guard_default_sampler_reads_host_proc_not_used_in_tests():
    # Constructing with defaults must not touch /proc until run() is called.
    MemoryGuard(CFG, FakeDeck(), lambda s: None)
