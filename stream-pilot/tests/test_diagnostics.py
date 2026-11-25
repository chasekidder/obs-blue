import asyncio
import json
from pathlib import Path

import pytest

from fakes import FakeSystem
from stream_pilot import diagnostics as D
from stream_pilot.config import load_config

# Captured on the stream PC (2026-11-25).
IW_STATION = """Station 54:47:cc:1a:c3:86 (on wlp4s0)
	authorized:	yes
	inactive time:	1 ms
	rx bytes:	807650589
	tx bytes:	1096945080
	tx retries:	9425
	tx failed:	0
	beacon loss:	0
		beacon rx:	30
	signal:  	-58 [-60, -58] dBm
	signal avg:	-58 dBm
	beacon signal avg:	-56 dBm
	tx bitrate:	612.5 MBit/s 80MHz HE-MCS 6 HE-NSS 2 HE-GI 1 HE-DCM 0
	rx bitrate:	720.6 MBit/s 80MHz HE-MCS 7 HE-NSS 2 HE-GI 0 HE-DCM 0
	expected throughput:	522.460Mbps
"""
IW_LINK = """Connected to 54:47:cc:1a:c3:86 (on wlp4s0)
	SSID: Example
	freq: 5745.0
	signal: -58 dBm
"""
SS = (
    "0      38107  [2607:fb92::8836]:38548  [2a03:2880:f356:95:face:b00c:0:1411]:443  "
    'users:(("obs",pid=2915,fd=53))\n'
    "\t cubic wscale:8,10 rto:262 rtt:61.801/5.681 ato:40 mss:1364 cwnd:135 ssthresh:57 "
    "bytes_sent:758999543 bytes_retrans:13640 bytes_acked:758961527 segs_out:559244 "
    "send 23836507bps lastsnd:22 pacing_rate 28603576bps delivery_rate 16118704bps "
    "app_limited busy:1331175ms unacked:28 retrans:0/10 reordering:5 notsent:90 minrtt:39.495\n"
    "0      0  127.0.0.1:4455  127.0.0.1:50000  "
    'users:(("obs",pid=2915,fd=60))\n'
    "\t cubic rtt:0.05/0.01 bytes_sent:999999999999\n"
)
PROC_STAT = "cpu  119727 4497 32154 1052641 1485 9264 5545 0 0 0\ncpu0 1 2 3 4 5 6 7 0 0 0\n"
PID_STAT = (
    "2915 (obs) S 2160 2915 2915 0 -1 4194560 1941314 459 166 0 70381 5988 0 0 20 0 26 0 "
    "2071 3401633792 80318 18446744073709551615 0 0 0"
)
NET_DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 41709082  172566    0    0    0     0          0         0 41709082  172566    0    0    0     0       0          0
wlp4s0: 1000 3 0 0 0 0 0 0 5000 225 0 0 0 0 0 0
"""
ROUTE = "default via 192.168.12.1 dev wlp4s0 proto dhcp src 192.168.12.121 metric 600 \n"
PING = "64 bytes from 192.168.12.1: icmp_seq=1 ttl=64 time=2.31 ms\n"


def test_parse_iw_station():
    w = D.parse_iw_station(IW_STATION)
    assert w == {
        "signal_dbm": -58.0, "signal_avg_dbm": -58.0, "tx_mbit": 612.5, "rx_mbit": 720.6,
        "tx_retries": 9425.0, "tx_failed": 0.0, "beacon_loss": 0.0, "expected_mbit": 522.46,
    }


def test_parse_ss_picks_the_remote_obs_socket():
    r = D.parse_ss_obs(SS)
    assert r["peer"].endswith(":443")
    assert r["rtt_ms"] == 61.801 and r["minrtt_ms"] == 39.495
    assert r["retrans_total"] == 10 and r["bytes_retrans"] == 13640
    assert r["delivery_bps"] == 16118704 and r["send_bps"] == 23836507
    assert r["notsent"] == 90 and r["unacked"] == 28 and r["cwnd"] == 135
    assert D.parse_ss_obs("") is None


def test_small_parsers():
    assert D.parse_proc_stat_cpu(PROC_STAT) == (119727 + 4497 + 32154 + 9264 + 5545, 1225313)
    assert D.parse_pid_stat(PID_STAT) == ("obs", 70381 + 5988, 80318)
    assert D.parse_pid_stat("12 (a (weird) name) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22")[0] == "a (weird) name"
    assert D.parse_net_dev(NET_DEV)["wlp4s0"] == (1000, 5000)
    assert D.parse_default_route(ROUTE) == ("192.168.12.1", "wlp4s0")
    assert D.parse_default_route("") == (None, None)
    assert D.parse_ping(PING) == 2.31 and D.parse_ping("1 packets transmitted, 0 received") is None
    assert D.parse_psi_all("some avg10=0.38 avg60=0.69 avg300=0.59 total=1\nfull avg10=0.00 avg60=0.10 avg300=0 total=0") == {
        "some_avg10": 0.38, "some_avg60": 0.69, "full_avg10": 0.0, "full_avg60": 0.1}


@pytest.mark.parametrize("comm, argv, group", [
    ("obs", [], "obs"),
    ("chromium-browse", ["/usr/lib64/chromium-browser/chromium-browser", "--type=renderer"], "chromium-renderer"),
    ("chromium-browse", ["/usr/lib64/chromium-browser/chromium-browser", "--type=gpu-process"], "chromium-other"),
    ("kwin_wayland", [], "kwin_wayland"),
    ("pipewire-pulse", [], "pipewire"),
    ("stream-pilot", ["/usr/lib/stream-pilot/venv/bin/python3", "/usr/bin/stream-pilot", "run"], "stream-pilot-run"),
    ("stream-pilot", ["/usr/lib/stream-pilot/venv/bin/python3", "/usr/bin/stream-pilot", "diag", "/x"], "stream-pilot-diag"),
    ("bash", ["bash"], None),
])
def test_process_group(comm, argv, group):
    assert D.process_group(comm, argv) == group


def test_scrub_replaces_stream_keys():
    assert D.scrub("key FB-1234567890123456-0-AbCdEfGh ok") == "key FB-<redacted> ok"


# ------------------------------------------------------------------ collector


def make_cfg(tmp_path, **diag):
    site = tmp_path / "site.toml"
    lines = ["[diagnostics]", "enabled = true"] + [f"{k} = {json.dumps(v)}" for k, v in diag.items()]
    site.write_text("\n".join(lines) + "\n")
    return load_config([site])


def fake_roots(tmp_path, *, cpu_ticks=0, busy=0, obs_ticks=0):
    proc, sys_ = tmp_path / "proc", tmp_path / "sys"
    (proc / "2915").mkdir(parents=True, exist_ok=True)
    (proc / "pressure").mkdir(exist_ok=True)
    (proc / "net").mkdir(exist_ok=True)
    (proc / "stat").write_text(f"cpu  {busy} 0 0 {cpu_ticks - busy} 0 0 0 0 0 0\n")
    (proc / "loadavg").write_text("0.52 0.40 0.30 1/300 4000\n")
    (proc / "meminfo").write_text("MemTotal: 16000000 kB\nMemAvailable: 12000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    for k in ("cpu", "memory", "io"):
        (proc / "pressure" / k).write_text("some avg10=1.00 avg60=0.50 avg300=0 total=0\n")
    (proc / "net" / "dev").write_text(NET_DEV)
    (proc / "2915" / "stat").write_text(
        f"2915 (obs) S 1 1 1 0 -1 0 0 0 0 0 {obs_ticks} 0 0 0 20 0 26 0 1 1 1000 0 0"
    )
    hw = sys_ / "class/hwmon/hwmon3"
    hw.mkdir(parents=True, exist_ok=True)
    (hw / "name").write_text("coretemp\n")
    (hw / "temp1_input").write_text("50000\n")
    (hw / "temp2_input").write_text("61000\n")
    gpu = sys_ / "class/drm/card1"
    gpu.mkdir(parents=True, exist_ok=True)
    (gpu / "gt_act_freq_mhz").write_text("350\n")
    (sys_ / "class/net/wlp4s0/wireless").mkdir(parents=True, exist_ok=True)
    return proc, sys_


class FakeRun:
    def __init__(self, journal="journal line\n"):
        self.calls = []
        self.journal = journal

    async def __call__(self, argv, timeout=5.0):
        self.calls.append(argv)
        if argv[:3] == ["ip", "route", "show"]:
            return ROUTE
        if argv[:2] == ["iw", "dev"] and argv[-1] == "dump":
            return IW_STATION
        if argv[:2] == ["iw", "dev"]:
            return IW_LINK
        if argv[0] == "ss":
            return SS
        if argv[0] == "ping":
            return PING
        if argv[0] == "journalctl":
            return self.journal
        raise AssertionError(argv)


class FakeObsStats:
    def __init__(self):
        self.n = 0

    async def request(self, kind, data=None):
        if kind == "GetStats":
            self.n += 1
            return {"activeFps": 30.0, "averageFrameRenderTime": 1.5, "cpuUsage": 6.0,
                    "renderSkippedFrames": 0, "renderTotalFrames": 100 * self.n,
                    "outputSkippedFrames": 0, "outputTotalFrames": 100 * self.n}
        if kind == "GetStreamStatus":
            return {"outputActive": True, "outputReconnecting": False,
                    "outputSkippedFrames": 25 * (self.n - 1), "outputTotalFrames": 100 * self.n,
                    "outputBytes": 1_000_000 * self.n, "outputCongestion": 0.0}
        if kind == "GetCurrentProgramScene":
            return {"currentProgramSceneName": "Main Camera"}
        raise AssertionError(kind)


async def test_system_samples_cpu_and_process_groups(tmp_path):
    proc, sys_ = fake_roots(tmp_path, cpu_ticks=1000, busy=100, obs_ticks=0)
    t = [1000.0]
    c = D.Collector(tmp_path / "s", make_cfg(tmp_path), proc_root=proc, sys_root=sys_,
                    clock=lambda: t[0], run=FakeRun())
    c._clk_tck = 100
    c._page_kb = 4
    await c.sample_system()
    fake_roots(tmp_path, cpu_ticks=2000, busy=600, obs_ticks=500)  # 10 s later: obs used 5 s of CPU
    t[0] += 10
    await c.sample_system()
    c._files["system.jsonl"].close()
    rows = [json.loads(x) for x in (tmp_path / "s/diag/system.jsonl").read_text().splitlines()]
    assert "cpu_pct" not in rows[0]
    r = rows[1]
    assert r["cpu_pct"] == 50.0
    assert r["procs"]["obs"] == {"n": 1, "rss_mb": round(1000 * 4 / 1024, 1), "cpu_pct": 50.0}
    assert r["mem_available_mb"] == 11718
    assert r["temps_c"] == {"coretemp": 61.0}
    assert r["gpu_mhz"] == 350.0
    assert r["psi_io"]["some_avg10"] == 1.0


async def test_network_sample(tmp_path):
    proc, sys_ = fake_roots(tmp_path)
    t = [1000.0]
    c = D.Collector(tmp_path / "s", make_cfg(tmp_path), proc_root=proc, sys_root=sys_,
                    clock=lambda: t[0], run=FakeRun())
    await c.sample_network()
    (proc / "net/dev").write_text(NET_DEV.replace("1000 3", "126000 3").replace("5000 225", "630000 225"))
    t[0] += 5
    await c.sample_network()
    c._files["network.jsonl"].close()
    rows = [json.loads(x) for x in (tmp_path / "s/diag/network.jsonl").read_text().splitlines()]
    r = rows[1]
    assert r["dev"] == "wlp4s0" and r["tx_kbps"] == 1000.0 and r["rx_kbps"] == 200.0
    assert r["wifi"]["signal_dbm"] == -58.0 and r["wifi"]["freq_mhz"] == 5745.0
    assert r["rtmp"]["rtt_ms"] == 61.801
    assert r["gw_ping_ms"] == 2.31


async def test_run_until_stopped_then_exports_and_summarizes(tmp_path, monkeypatch):
    proc, sys_ = fake_roots(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    logs = tmp_path / "home/.config/obs-studio/logs"
    logs.mkdir(parents=True)
    (logs / "2026-11-25 18-06-11.txt").write_text("rtmp key FB-1234567890123456-0-Secret\n")
    run = FakeRun(journal="INFO stream_pilot.controller: state -> LIVE\nkey=FB-1234567890123456-0-Secret\n")
    stream = tmp_path / "s"
    stream.mkdir()
    (stream / "comments.jsonl").write_text(
        json.dumps({"source": "fb_dom", "captured_at": 1795648000.0,
                    "created_at": "2026-11-25T23:06:38+00:00", "author": "a", "text": "t"}) + "\n")
    c = D.Collector(stream, make_cfg(tmp_path, obs_s=0.5, network_s=0.5, system_s=0.5),
                    obs=FakeObsStats(), proc_root=proc, sys_root=sys_, run=run)
    task = asyncio.create_task(c.run())
    await asyncio.sleep(1.3)
    c.stop()
    await asyncio.wait_for(task, 10)
    d = stream / "diag"
    assert {"obs.jsonl", "network.jsonl", "system.jsonl", "events.jsonl", "summary.txt",
            "journal-user.txt", "journal-system.txt", "obs-2026-11-25_18-06-11.txt"} <= {p.name for p in d.iterdir()}
    assert "Secret" not in (d / "journal-user.txt").read_text()
    assert "FB-<redacted>" in (d / "obs-2026-11-25_18-06-11.txt").read_text()
    summary = (d / "summary.txt").read_text()
    assert "dropped (network): " in summary and "frames in" in summary
    assert "state changes: 1" in summary
    assert "lines by source: fb_dom 1" in summary
    events = [json.loads(x) for x in (d / "events.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events] == ["collector_started", "collector_stopped"]


async def test_a_failing_sampler_never_stops_the_others(tmp_path, caplog):
    proc, sys_ = fake_roots(tmp_path)

    class BrokenObs:
        async def request(self, kind, data=None):
            raise ConnectionRefusedError("obs down")

    c = D.Collector(tmp_path / "s", make_cfg(tmp_path, obs_s=0.5, network_s=0.5, system_s=0.5),
                    obs=BrokenObs(), proc_root=proc, sys_root=sys_, run=FakeRun())
    task = asyncio.create_task(c.run())
    await asyncio.sleep(1.2)
    c.stop()
    await asyncio.wait_for(task, 10)
    assert (tmp_path / "s/diag/system.jsonl").read_text().count("\n") >= 2
    assert "diagnostics: obs failed (1 times)" in caplog.text


def test_summary_lists_bursts(tmp_path):
    d = tmp_path / "s/diag"
    d.mkdir(parents=True)
    rows = []
    for i in range(10):
        rows.append({"t": 1795648000 + 2 * i, "renderSkippedFrames": 25 if i >= 5 else 0,
                     "outputSkippedFrames": 0, "net_outputSkippedFrames": 59 if i >= 7 else 0,
                     "net_outputBytes": 1_150_000 * i, "averageFrameRenderTime": 1.5,
                     "net_outputActive": True, "scene": "Main Camera"})
    (d / "obs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    s = D.summarize(tmp_path / "s")
    assert "skipped (rendering lag): 25 frames in 1 sample(s)" in s
    assert "dropped (network): 59 frames in 1 sample(s)" in s
    assert "output bitrate: min 4600" in s
    assert "program scenes (samples): Main Camera 10" in s


# ---------------------------------------------------------------- the feature


async def test_feature_starts_and_stops_the_collector(tmp_path):
    system = FakeSystem()
    f = D.Diagnostics(make_cfg(tmp_path), system=system)
    assert f.enabled
    stream = tmp_path / "streams/2026-11-25_1800-1"
    unit = D.diag_unit(stream)
    assert unit.startswith("stream-pilot-diag@") and unit.endswith(".service")
    await f.live_confirmed("1", stream, None)
    assert ("system.start_user_unit", unit) in system.calls
    system.active_units.discard(unit)
    await f.live_resumed("1", stream)
    assert system.calls.count(("system.start_user_unit", unit)) == 2
    await f.stream_stopped(stream, 0.0)
    assert system.calls[-1] == ("system.stop_user_unit_no_block", unit)
    await f.restore()
    assert system.calls[-1] == ("system.stop_user_unit", D.DIAG_UNITS)


def test_shipped_off_and_listed_as_a_feature(tmp_path):
    cfg = load_config([])
    assert cfg.diagnostics.enabled is False
    assert "diagnostics" in cfg.features()
