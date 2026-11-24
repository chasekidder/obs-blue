"""[recording]: the recorder unit, stream.json and DONE, through the controller."""

import asyncio
import json
import time

import pytest

from stream_pilot import controller as controller_mod
from stream_pilot import stream_meta
from stream_pilot.events import DeckPress, PowerPress
from stream_pilot.persist import Saved
from stream_pilot.recording import RECORDER_UNITS, Recording, recorder_unit
from stream_pilot.states import State
from stream_pilot.system import escape_unit_path

from fakes import FakeObs
from test_controller import Rig, make  # noqa: F401 (the rig fixture)

ON = "\n[recording]\nenabled = true\nfb_delay_s = 12\n"


class FakeExec:
    """asyncio.create_subprocess_exec stand-in for rpm-ostree, timedatectl and chronyc."""

    OUT = {
        "rpm-ostree": json.dumps({"deployments": [{
            "container-image-reference-digest": "sha256:abc",
            "version": "44.20261124.1",
            "container-image-reference": "ostree-unverified-registry:ghcr.io/x/obs-blue:stable",
        }]}),
        "timedatectl": "yes\n",
        "chronyc": "Reference ID    : 0A000001\nSystem time     : 0.001054459 seconds slow of NTP time\n",
    }

    def __init__(self):
        self.argvs = []

    async def __call__(self, *argv, **_kw):
        self.argvs.append(argv)
        out = self.OUT[argv[0]].encode()

        class Proc:
            returncode = 0

            async def communicate(self):
                return out, b""

        return Proc()


def recording(rig, exec_fn=None):
    return [Recording(rig.cfg, obs=rig.obs, system=rig.system, exec_fn=exec_fn or FakeExec())]


def stream_dir(rig):
    return rig.c.stream_dir


# ------------------------------------------------------------------ switch off


async def test_off_starts_no_recorder_and_writes_no_stream_json(make):
    rig = make(features=recording)
    await rig.to_live()
    sd = stream_dir(rig)
    assert not any(c[0] == "system.start_user_unit" for c in rig.system.calls)
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert not (sd / "stream.json").exists()
    assert not (sd / "DONE").exists()
    assert ("system.poweroff",) in rig.system.calls
    await rig.c.aclose()


async def test_off_stops_a_leftover_recorder_at_boot(make):
    rig = make(features=recording)
    rig.system.active_units.add("stream-pilot-recorder@home-x.service")
    await rig.c.boot()
    await rig.settle()
    assert rig.system.active_units == set()
    await rig.c.aclose()


# ------------------------------------------------------------------- switch on


async def test_go_live_starts_the_recorder_and_writes_stream_json(make):
    rig = make(features=recording, site_extra=ON)
    rig.obs.model.stream_status["outputDuration"] = 125_000
    before = time.time()
    await rig.to_live()
    sd = stream_dir(rig)
    unit = recorder_unit(sd)
    assert unit in rig.system.active_units
    assert unit == f"stream-pilot-recorder@{escape_unit_path(str(sd))}.service"
    meta = json.loads((sd / "stream.json").read_text())
    assert meta["live_id"] == rig.fb.live_id
    assert before <= meta["go_live_pressed_at"] <= meta["live_confirmed_at"]
    assert meta["live_confirmed_at"] - meta["preroll_started_at"] == pytest.approx(125, abs=1)
    assert meta["mic_device"] == rig.cfg.audio.mic_device
    assert meta["fb_delay_s"] == 12
    assert meta["replay_includes_preroll"] is rig.cfg.recording.replay_includes_preroll
    assert meta["image"] == {"digest": "sha256:abc", "version": "44.20261124.1",
                             "reference": "ostree-unverified-registry:ghcr.io/x/obs-blue:stable"}
    assert meta["clock_at_live"] == {"ntp_synchronized": True, "offset_s": 0.001054459}
    assert "stream_pilot_version" in meta
    assert not (sd / "DONE").exists()
    await rig.c.aclose()


async def test_end_stops_the_recorder_before_the_capture_then_done_last(make):
    rig = make(features=recording, site_extra=ON, shared=True)
    await rig.to_live()
    sd = stream_dir(rig)
    rig.clear()
    await rig.c.handle(PowerPress())
    await rig.settle()
    names = [c[0] if c[0] != "system.stop_user_unit" else c for c in rig.shared]
    stop_rec = names.index(("system.stop_user_unit", recorder_unit(sd)))
    assert stop_rec < names.index("capture.stop")
    assert names.index("obs.stop_stream") < stop_rec
    meta = json.loads((sd / "stream.json").read_text())
    assert meta["ended_at"] >= meta["live_confirmed_at"]
    assert "clock_at_end" in meta
    done = sd / "DONE"
    assert done.exists()
    newest = max(p.stat().st_mtime_ns for p in sd.iterdir() if p.name != "DONE")
    assert done.stat().st_mtime_ns >= newest
    assert recorder_unit(sd) not in rig.system.active_units
    await rig.c.aclose()


async def test_long_press_end_also_finishes_the_folder(make):
    rig = make(features=recording, site_extra=ON)
    await rig.to_live()
    sd = stream_dir(rig)
    await rig.c.handle(DeckPress(0, long=True))
    await rig.settle()
    assert (sd / "DONE").exists()
    assert rig.c.state == State.READY
    await rig.c.aclose()


async def test_resume_restarts_a_missing_recorder(make, tmp_path):
    rig = make(features=recording, site_extra=ON)
    sd = rig.cfg.paths.streams_dir / "2026-11-24_1500-42"
    rig.store.save(Saved("LIVE", "42", str(sd)))
    rig.obs.active = True
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert recorder_unit(sd) in rig.system.active_units
    meta = json.loads((sd / "stream.json").read_text())
    assert len(meta["resumed_at"]) == 1
    await rig.c.aclose()


async def test_resume_leaves_a_running_recorder_alone(make):
    rig = make(features=recording, site_extra=ON)
    sd = rig.cfg.paths.streams_dir / "2026-11-24_1500-42"
    rig.store.save(Saved("LIVE", "42", str(sd)))
    rig.obs.active = True
    rig.system.active_units.add(recorder_unit(sd))
    await rig.c.boot()
    await rig.settle()
    assert ("system.start_user_unit", recorder_unit(sd)) not in rig.system.calls
    assert ("system.stop_user_unit", RECORDER_UNITS) not in rig.system.calls
    await rig.c.aclose()


async def test_boot_without_a_live_stops_a_stale_recorder(make):
    rig = make(features=recording, site_extra=ON)
    rig.system.active_units.add("stream-pilot-recorder@home-old.service")
    await rig.c.boot()
    await rig.settle()
    assert rig.system.active_units == set()
    await rig.c.aclose()


async def test_broken_recording_never_stops_go_live_or_the_end(make, monkeypatch):
    from stream_pilot import features as features_mod

    monkeypatch.setattr(features_mod, "HOOK_TIMEOUT_S", 0.05)
    rig = make(features=recording, site_extra=ON)
    rig.system.gates["start_user_unit"] = asyncio.Event()  # hangs
    await rig.to_live()
    assert rig.c.state == State.LIVE
    assert ("fb.open_comments", rig.fb.live_id) in rig.fb.calls
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert ("system.poweroff",) in rig.system.calls
    await rig.c.aclose()


# ----------------------------------------------------------------- stream_meta


def test_chrony_offset_sign():
    assert stream_meta.parse_chrony_offset("System time     : 0.5 seconds slow of NTP time") == 0.5
    assert stream_meta.parse_chrony_offset("System time     : 0.25 seconds fast of NTP time") == -0.25
    assert stream_meta.parse_chrony_offset("nothing") is None


async def test_clock_info_without_chrony():
    async def missing(*argv, **kw):
        raise FileNotFoundError(argv[0])

    assert await stream_meta.clock_info(missing) == {"ntp_synchronized": None, "offset_s": None}


def test_update_skips_none_and_keeps_other_fields(tmp_path):
    stream_meta.update(tmp_path, live_id="1", ended_at=None)
    stream_meta.update(tmp_path, ended_at=5.0)
    stream_meta.append_to(tmp_path, "resumed_at", 3.0)
    assert stream_meta.read(tmp_path) == {"live_id": "1", "ended_at": 5.0, "resumed_at": [3.0]}


def test_escape_unit_path_matches_systemd_escape():
    assert escape_unit_path("/home/stream/streams/2026-11-24_1530-1234567") == (
        "home-stream-streams-2026\\x2d11\\x2d24_1530\\x2d1234567"
    )
    assert escape_unit_path("/home/a b/.x/é") == "home-a\\x20b-.x-\\xc3\\xa9"
    assert escape_unit_path("/") == "-"
    assert escape_unit_path("/a//b/") == "a-b"


# ------------------------------------------------------------- end-flow budget


def test_end_flow_budget_fits_the_power_guard():
    from stream_pilot import power_guard

    tail = 2 * controller_mod.END_TAIL_MIN_S
    assert controller_mod.END_FLOW_BUDGET_S + tail <= power_guard.GRACE_S - 15
    from stream_pilot.config import load_config

    cfg = load_config([])
    stop_phase = (controller_mod.END_STEP_TIMEOUT_S  # StopStream
                  + max(cfg.timeouts.stop_s, controller_mod.END_STEP_TIMEOUT_S)
                  + controller_mod.STOP_POLL_S  # waiting for OBS to stop
                  + cfg.timeouts.fb_end_wait_s)
    assert stop_phase + controller_mod.END_TAIL_RESERVE_S <= controller_mod.END_FLOW_BUDGET_S


async def test_worst_case_end_flow_stays_inside_the_budget(make, monkeypatch):
    """Everything hangs (OBS, the capture, Chromium, the recorder): scaled 1:100."""
    scale = 0.01
    for name in ("END_STEP_TIMEOUT_S", "END_FLOW_BUDGET_S", "END_TAIL_RESERVE_S",
                 "END_TAIL_MIN_S", "END_HOOK_TIMEOUT_S"):
        monkeypatch.setattr(controller_mod, name, getattr(controller_mod, name) * scale)
    from stream_pilot import features as features_mod

    monkeypatch.setattr(features_mod, "HOOK_TIMEOUT_S", features_mod.HOOK_TIMEOUT_S * scale)
    rig = make(features=recording, site_extra=ON)
    await rig.to_live()
    hang = asyncio.Event()
    for obj, names in ((rig.obs, ["stop_stream", "stream_active"]),
                       (rig.capture, ["stop"]),
                       (rig.fb, ["close_browser"]),
                       (rig.system, ["stop_user_unit"])):
        for n in names:
            obj.gates[n] = hang
    started = time.monotonic()
    await rig.c.handle(PowerPress())
    await asyncio.wait_for(rig.c.wait_idle(), 5)
    elapsed = time.monotonic() - started
    print(f"scaled worst-case end flow: {elapsed:.3f}s")
    assert ("system.poweroff",) in rig.system.calls
    from stream_pilot import power_guard

    assert elapsed < (power_guard.GRACE_S - 15) * scale
    await rig.c.aclose()
