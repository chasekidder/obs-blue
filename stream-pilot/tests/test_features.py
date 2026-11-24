"""The feature-switch plumbing: config, status listing, controller hooks."""

import asyncio
import json

from stream_pilot import features as features_mod
from stream_pilot.config import FEATURE_SECTIONS, load_config
from stream_pilot.controller import Controller
from stream_pilot.events import ObsConnected, Tick
from stream_pilot.features import Feature, describe
from stream_pilot.health import format_status
from stream_pilot.persist import StateStore, StatusWriter
from stream_pilot.states import State

from fakes import FakeCamera, FakeCapture, FakeDeck, FakeFacebook, FakeObs, FakeSystem


class Probe(Feature):
    name = "probe"

    def __init__(self, enabled, *, fail=False, hang=False):
        super().__init__(enabled)
        self.calls: list[str] = []
        self.fail = fail
        self.hang = hang

    async def restore(self):
        self.calls.append("restore")

    async def obs_connected(self):
        self.calls.append("obs_connected")
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise RuntimeError("feature broke")


def make(tmp_path, features=(), site=""):
    path = tmp_path / "site.toml"
    path.write_text(
        '[facebook]\nactor_id = "1"\ngroup_id = "2"\ngroup_name = "G"\n'
        f'[paths]\nstate_dir = "{tmp_path}/state"\nstreams_dir = "{tmp_path}/streams"\n'
        f'failures_dir = "{tmp_path}/failures"\n'
        "[timeouts]\npreview_s = 0.2\nlive_confirm_s = 0.2\nstop_s = 0.2\nfb_end_wait_s = 0\n"
        + site
    )
    cfg = load_config([path])

    async def no_sleep(_s):
        await asyncio.sleep(0)

    c = Controller(
        cfg, obs=FakeObs(), fb=FakeFacebook(), capture=FakeCapture(), deck=FakeDeck(),
        system=FakeSystem(), camera=FakeCamera(), store=StateStore(cfg.paths.state_dir / "s.json"),
        status=StatusWriter(cfg.paths.state_dir / "status.json"), sleep=no_sleep,
        features=features,
    )
    return c, cfg


def test_every_feature_section_has_a_boolean_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config([])
    switches = cfg.features()
    assert list(switches) == list(FEATURE_SECTIONS)
    assert all(isinstance(v, bool) for v in switches.values())
    assert switches["ssn"] is True


def test_non_boolean_switch_falls_back_to_the_default(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HOME", str(tmp_path))
    site = tmp_path / "site.toml"
    site.write_text('[ssn]\nenabled = "false"\n')
    cfg = load_config([site])
    assert cfg.ssn.enabled is True
    assert "ssn.enabled must be true or false" in caplog.text


def test_describe():
    assert describe({"a": True, "b": False}) == "a on, b off"
    assert describe({}) == "-"


def test_status_lists_every_feature():
    text = format_status({"state": "READY", "features": {"ssn": True, "recording": False}},
                         now=0)
    assert text.endswith("features:\n  ssn: on\n  recording: off")


def test_status_without_features_is_unchanged():
    # status.json written by an older image (after a rollback) has no "features"
    text = format_status({"state": "READY"}, now=0)
    assert "features" not in text


async def test_status_json_carries_the_switches(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    c, cfg = make(tmp_path)
    await c.handle(Tick())
    status = json.loads((cfg.paths.state_dir / "status.json").read_text())
    assert status["features"] == cfg.features()


async def test_obs_connect_runs_enabled_features_and_restores_disabled_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    on, off = Probe(True), Probe(False)
    c, _ = make(tmp_path, [on, off])
    await c.handle(ObsConnected())
    await asyncio.wait_for(c.wait_idle(), 2)
    assert on.calls == ["obs_connected"]
    assert off.calls == ["restore"]
    await c.aclose()


async def test_boot_restores_disabled_features_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    on, off = Probe(True), Probe(False)
    c, _ = make(tmp_path, [on, off])
    await c.boot()
    await asyncio.wait_for(c.wait_idle(), 2)
    assert off.calls == ["restore"]
    assert on.calls == []
    assert c.state == State.READY
    await c.aclose()


async def test_a_failing_feature_never_reaches_error(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HOME", str(tmp_path))
    bad, good = Probe(True, fail=True), Probe(True)
    c, _ = make(tmp_path, [bad, good])
    await c.boot()
    await c.handle(ObsConnected())
    await asyncio.wait_for(c.wait_idle(), 2)
    assert c.state == State.READY
    assert good.calls == ["obs_connected"]
    assert "feature probe: obs_connected failed" in caplog.text
    await c.aclose()


async def test_a_hung_feature_never_blocks_ready(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(features_mod, "HOOK_TIMEOUT_S", 0.05)
    hung = Probe(True, hang=True)
    c, _ = make(tmp_path, [hung])
    await c.handle(ObsConnected())
    await c.boot()
    await asyncio.wait_for(c.wait_idle(), 2)
    assert c.state == State.READY
    assert "timed out" in caplog.text
    await c.aclose()


async def test_reconnect_during_a_sync_runs_it_again(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    gate = asyncio.Event()

    class Slow(Probe):
        async def obs_connected(self):
            self.calls.append("obs_connected")
            await gate.wait()

    f = Slow(True)
    c, _ = make(tmp_path, [f])
    await c.handle(ObsConnected())
    await asyncio.sleep(0)
    await c.handle(ObsConnected())
    await c.handle(ObsConnected())
    gate.set()
    await asyncio.wait_for(c.wait_idle(), 2)
    assert f.calls == ["obs_connected", "obs_connected"]
    await c.aclose()


async def test_no_features_means_no_obs_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    c, _ = make(tmp_path)
    await c.boot()
    await c.handle(ObsConnected())
    await asyncio.wait_for(c.wait_idle(), 2)
    assert c.obs.requests == []
    await c.aclose()
