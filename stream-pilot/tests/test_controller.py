import asyncio
import json
import re
import threading
from pathlib import Path

import pytest

from stream_pilot.comment_log import build_report
from stream_pilot.config import load_config
from stream_pilot.controller import Controller
from stream_pilot.events import (
    DeckPress,
    ObsStreamState,
    PowerPress,
    RetryRequested,
    Tick,
)
from stream_pilot.persist import Saved, StateStore, StatusWriter
from stream_pilot.ports import StepFailed
from stream_pilot.states import LIVE_STATES, STATUS_FACES, KeyFace, State

from fakes import (
    Script,
    FakeCamera,
    FakeCapture,
    FakeDeck,
    FakeFacebook,
    FakeObs,
    FakeSystem,
)

SITE = """
[facebook]
actor_id = "111"
group_id = "222"
group_name = "Test Group"

[paths]
state_dir = "{root}/state"
streams_dir = "{root}/streams"
failures_dir = "{root}/failures"

[timeouts]
attempts = 2
preview_s = 0.25
live_confirm_s = 0.5
stop_s = 1
fb_end_wait_s = {end_wait}
net_retry_s = 0.25
reconnect_retry_s = 0.75
"""

EMPTY_SITE = """
[paths]
state_dir = "{root}/state"
streams_dir = "{root}/streams"
failures_dir = "{root}/failures"
"""


class Sleeper:
    def __init__(self, shared: list | None = None):
        self.calls: list[float] = []
        self.shared = shared

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.shared is not None:
            self.shared.append(("sleep", seconds))
        await asyncio.sleep(0)


class RecordingStore(StateStore):
    def __init__(self, path):
        super().__init__(path)
        self.history: list[Saved] = []

    def save(self, saved: Saved) -> None:
        self.history.append(saved)
        super().save(saved)

    @property
    def states(self) -> list[str]:
        return [s.state for s in self.history]


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        empty_site=False,
        dry_run=False,
        shared=False,
        end_wait=0,
        obs=None,
        fb=None,
        capture=None,
        system=None,
        camera=None,
    ):
        tpl = EMPTY_SITE if empty_site else SITE
        site = tmp_path / "site.toml"
        site.write_text(tpl.format(root=tmp_path.as_posix(), end_wait=end_wait))
        self.cfg = load_config([site])
        self.shared: list[tuple] | None = [] if shared else None
        self.obs = obs or FakeObs(calls=self.shared)
        self.fb = fb or FakeFacebook(calls=self.shared)
        self.capture = capture or FakeCapture(calls=self.shared)
        self.system = system or FakeSystem(calls=self.shared)
        self.camera = camera or FakeCamera()
        self.deck = FakeDeck()
        self.store = RecordingStore(self.cfg.paths.state_dir / "state.json")
        self.status_path = self.cfg.paths.state_dir / "status.json"
        self.sleeper = Sleeper(self.shared)
        self.c = Controller(
            self.cfg,
            obs=self.obs,
            fb=self.fb,
            capture=self.capture,
            deck=self.deck,
            system=self.system,
            camera=self.camera,
            store=self.store,
            status=StatusWriter(self.status_path),
            dry_run=dry_run,
            sleep=self.sleeper,
        )

    @property
    def state_json(self) -> dict:
        return json.loads((self.cfg.paths.state_dir / "state.json").read_text())

    @property
    def status_json(self) -> dict:
        return json.loads(self.status_path.read_text())

    def names(self, obj) -> list[str]:
        return [c[0] for c in obj.calls]

    async def settle(self) -> None:
        await asyncio.wait_for(self.c.wait_idle(), 2)

    async def wait_state(self, state: State) -> None:
        for _ in range(2000):
            if self.c.state == state:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"state stayed {self.c.state}, wanted {state}")

    async def to_ready(self) -> None:
        await self.c.boot()
        await self.settle()
        assert self.c.state == State.READY

    async def to_live(self) -> None:
        await self.to_ready()
        await self.c.handle(DeckPress(0))
        await self.settle()
        assert self.c.state == State.LIVE

    def clear(self) -> None:
        for obj in (self.obs, self.fb, self.capture, self.system, self.camera):
            obj.calls.clear()
        if self.shared is not None:
            self.shared.clear()


@pytest.fixture
async def make(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    rigs: list[Rig] = []

    def factory(**kw) -> Rig:
        rig = Rig(tmp_path, **kw)
        rigs.append(rig)
        return rig

    yield factory
    for rig in rigs:
        await rig.c.aclose()


@pytest.fixture
async def rig(make):
    return make()


# ---------------------------------------------------------------- boot / setup


async def test_boot_shows_all_six_faces(rig):
    await rig.c.boot()
    assert set(rig.deck.faces) == {0, 1, 2, 3, 4, 5}
    assert rig.deck.faces[3] == KeyFace("RETRY", "#333333")
    assert rig.deck.faces[4] == KeyFace("", "#000000")
    assert rig.deck.faces[1] == KeyFace("AF\nOFF", "#555555")
    assert rig.deck.faces[2] == KeyFace("MAIN\nCAM", "#333333")
    assert rig.deck.faces[5] == KeyFace("JEWEL\nCAM", "#333333")


async def test_happy_path_reaches_ready_in_order(make):
    rig = make(shared=True)
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.shared == [
        ("obs.connect",),
        ("fb.connect",),
        ("fb.reachable",),
        ("fb.logged_in",),
        ("fb.setup_live",),
        ("obs.set_stream_target", rig.fb.target),
        ("obs.stream_active",),
        ("obs.start_stream",),
        ("fb.wait_preview_ready", rig.cfg.timeouts.preview_s),
    ]
    assert rig.store.states == ["STARTING", "LOGIN", "SETUP", "READY"]
    assert rig.deck.faces[0] == STATUS_FACES[State.READY]
    assert rig.sleeper.calls == []


async def test_transition_writes_state_and_status_files(rig):
    await rig.to_ready()
    assert rig.state_json == {"state": "READY", "live_id": None, "stream_dir": None}
    status = rig.status_json
    assert status["state"] == "READY"
    assert status["live_id"] is None
    assert status["last_error"] is None
    assert isinstance(status["since"], float)
    assert isinstance(status["updated_at"], float)
    assert status["memory"] is None


async def test_connect_retried_every_two_seconds_until_both_succeed(make):
    rig = make(
        obs=FakeObs(connect_raises=[ConnectionError("no obs"), None]),
        fb=FakeFacebook(connect_raises=[RuntimeError("no cdp"), RuntimeError("x"), None]),
    )
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.sleeper.calls == [2, 2]
    assert rig.obs.calls.count(("obs.connect",)) == 2  # one failure, then not redone once connected
    assert rig.fb.calls.count(("fb.connect",)) == 3


async def test_login_wait_rechecks_every_five_seconds(make):
    rig = make(fb=FakeFacebook(logged_in=[False, False, True]))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.sleeper.calls == [5, 5]
    assert "LOGIN" in rig.store.states
    assert rig.fb.calls.count(("fb.logged_in",)) == 3
    assert ("fb.setup_live",) in rig.fb.calls


async def test_login_stays_in_login_state_while_waiting(make):
    rig = make(fb=FakeFacebook(logged_in=False))
    await rig.c.boot()
    await rig.wait_state(State.LOGIN)
    assert rig.deck.faces[0] == STATUS_FACES[State.LOGIN]
    assert "fb.setup_live" not in rig.names(rig.fb)


async def test_login_check_failing_every_attempt_goes_to_error(make):
    rig = make(fb=FakeFacebook(logged_in=StepFailed("login_check", "boom")))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.ERROR
    assert rig.c.last_error == "login_check: boom"
    assert rig.fb.calls.count(("fb.logged_in",)) == 2  # attempts = 2


async def test_no_net_waits_for_reachable(make):
    rig = make(fb=FakeFacebook(reachable=[False, False, True]))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    assert "NO_NET" in rig.store.states
    assert rig.sleeper.calls == [0.25, 0.25]
    assert rig.names(rig.fb).index("fb.reachable") < rig.names(rig.fb).index("fb.logged_in")


async def test_no_net_not_entered_when_reachable(rig):
    await rig.to_ready()
    assert "NO_NET" not in rig.store.states


async def test_config_missing_goes_to_error_without_touching_facebook(make):
    rig = make(empty_site=True)
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.ERROR
    assert "facebook.actor_id" in rig.c.last_error
    assert "facebook.group_name" in rig.c.last_error
    assert rig.deck.faces[0].label == "ERR\nCONFIG"
    assert "fb.reachable" not in rig.names(rig.fb)
    assert "fb.save_failure" not in rig.names(rig.fb)
    assert rig.status_json["state"] == "ERROR"


async def test_setup_step_retried_then_succeeds(make):
    rig = make(fb=FakeFacebook(setup_raises=[StepFailed("setup", "flaky"), None]))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.fb.calls.count(("fb.setup_live",)) == 2
    assert "fb.save_failure" not in rig.names(rig.fb)


async def test_preview_failure_retries_whole_setup_phase(make):
    rig = make(fb=FakeFacebook(preview_raises=[StepFailed("preview"), None]))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.fb.calls.count(("fb.setup_live",)) == 2
    # second attempt found OBS still streaming from the first: stopped first (no live exists in SETUP)
    assert "obs.stop_stream" in rig.names(rig.obs)


async def test_setup_failure_after_all_attempts_goes_to_error(make):
    rig = make(fb=FakeFacebook(setup_raises=StepFailed("setup", "no button")))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.ERROR
    assert rig.fb.calls.count(("fb.setup_live",)) == 2
    saves = [c for c in rig.fb.calls if c[0] == "fb.save_failure"]
    assert len(saves) == 1
    _, step, directory = saves[0]
    assert step == "setup"
    assert directory.parent == rig.cfg.paths.failures_dir
    assert re.fullmatch(r"\d{8}-\d{6}-setup", directory.name)
    assert rig.c.last_error == "setup: no button"
    assert rig.deck.faces[0].label == "ERR\nSETUP"
    assert rig.deck.faces[0].bg == STATUS_FACES[State.ERROR].bg
    assert rig.state_json["state"] == "ERROR"
    assert rig.status_json["last_error"] == "setup: no button"
    assert "obs.start_stream" not in rig.names(rig.obs)


async def test_error_label_truncates_step_to_six_chars_uppercase(make):
    rig = make(fb=FakeFacebook(preview_raises=StepFailed("preview_ready")))
    await rig.c.boot()
    await rig.settle()
    assert rig.deck.faces[0].label == "ERR\nPREVIE"


async def test_unexpected_exception_becomes_internal_error(make):
    rig = make(fb=FakeFacebook(setup_raises=RuntimeError("kaboom")))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.ERROR
    assert rig.c.last_error.startswith("internal:")
    assert "kaboom" in rig.c.last_error
    assert rig.deck.faces[0].label == "ERR\nINTERN"
    assert rig.fb.calls[-1][:2] == ("fb.save_failure", "internal")


async def test_save_failure_exception_is_swallowed(make):
    rig = make(
        fb=FakeFacebook(
            setup_raises=StepFailed("setup"), save_failure_raises=RuntimeError("disk")
        )
    )
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.ERROR


async def test_setup_stops_already_active_obs_stream_first(make):
    rig = make(obs=FakeObs(active=True))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    names = rig.names(rig.obs)
    assert names.index("obs.set_stream_target") < names.index("obs.stop_stream")
    assert names.index("obs.stop_stream") < names.index("obs.start_stream")


# ---------------------------------------------------------------- retry


@pytest.mark.parametrize("event", [DeckPress(3), RetryRequested(), DeckPress(0)])
async def test_retry_from_error_reruns_setup_from_login(make, event):
    rig = make(fb=FakeFacebook(setup_raises=[StepFailed("setup")] * 2 + [None]))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.ERROR
    rig.clear()
    await rig.c.handle(event)
    await rig.settle()
    assert rig.c.state == State.READY
    names = rig.names(rig.fb)
    assert names[:2] == ["fb.reachable", "fb.logged_in"]
    assert "fb.setup_live" in names
    assert "obs.connect" not in rig.names(rig.obs)  # already connected


async def test_retry_cancels_running_flow_and_restarts(make):
    rig = make(fb=FakeFacebook(logged_in=False))
    await rig.c.boot()
    await rig.wait_state(State.LOGIN)
    rig.fb._logged_in = Script(True)
    await rig.c.handle(DeckPress(3))
    await rig.settle()
    assert rig.c.state == State.READY


@pytest.mark.parametrize("state", [State.GOING_LIVE, State.LIVE, State.RECONNECTING, State.ENDING])
@pytest.mark.parametrize("event", [DeckPress(3), RetryRequested()])
async def test_retry_ignored_in_live_states(rig, state, event):
    rig.c.state = state
    await rig.c.handle(event)
    await rig.settle()
    assert rig.c.state == state
    assert rig.fb.calls == []
    assert rig.obs.calls == []


async def test_retry_ignored_while_really_live(rig):
    await rig.to_live()
    rig.clear()
    await rig.c.handle(DeckPress(3))
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert rig.fb.calls == [] and rig.obs.calls == []


# ---------------------------------------------------------------- go live


async def test_go_live_sequence(make):
    rig = make(shared=True)
    await rig.to_ready()
    rig.clear()
    await rig.c.handle(DeckPress(0))
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert rig.c.live_id == "123456789"
    stream_dir = rig.shared[2][2]
    assert rig.shared == [
        ("fb.go_live", rig.cfg.timeouts.live_confirm_s),
        ("fb.open_comments", "123456789"),
        ("capture.start", "123456789", stream_dir),
        ("fb.park_dashboard",),
    ]
    assert stream_dir.is_dir()
    assert stream_dir.parent == rig.cfg.paths.streams_dir
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}-123456789", stream_dir.name)
    assert rig.store.states[-2:] == ["GOING_LIVE", "LIVE"]
    assert rig.state_json == {
        "state": "LIVE",
        "live_id": "123456789",
        "stream_dir": str(stream_dir),
    }
    assert rig.status_json["live_id"] == "123456789"
    assert rig.deck.faces[0] == STATUS_FACES[State.LIVE]
    assert "obs.stop_stream" not in rig.names(rig.obs)


async def test_press_during_go_live_flow_does_not_double_click(rig):
    await rig.to_ready()
    await rig.c.handle(DeckPress(0))
    assert rig.c.state == State.GOING_LIVE  # entered synchronously
    await rig.c.handle(DeckPress(0))  # ignored: not READY any more
    await rig.settle()
    assert rig.fb.calls.count(("fb.go_live", rig.cfg.timeouts.live_confirm_s)) == 1


async def test_go_live_failure_is_single_attempt_and_goes_to_error(make):
    rig = make(fb=FakeFacebook(go_live_raises=StepFailed("go_live", "no end button")))
    await rig.to_ready()
    rig.clear()
    await rig.c.handle(DeckPress(0))
    await rig.settle()
    assert rig.c.state == State.ERROR
    assert rig.fb.calls.count(("fb.go_live", rig.cfg.timeouts.live_confirm_s)) == 1
    assert [c[:2] for c in rig.fb.calls if c[0] == "fb.save_failure"] == [("fb.save_failure", "go_live")]
    assert rig.deck.faces[0].label == "ERR\nGO_LIV"
    assert rig.c.live_id is None
    assert "obs.stop_stream" not in rig.names(rig.obs)
    assert "capture.start" not in rig.names(rig.capture)


async def test_retry_after_go_live_failure_returns_to_ready_via_setup(make):
    rig = make(fb=FakeFacebook(go_live_raises=[StepFailed("go_live"), None]))
    await rig.to_ready()
    await rig.c.handle(DeckPress(0))
    await rig.settle()
    assert rig.c.state == State.ERROR
    assert rig.obs.active  # OBS stream was left running
    await rig.c.handle(DeckPress(3))
    await rig.settle()
    assert rig.c.state == State.READY


async def test_post_go_live_failures_still_end_in_live(make):
    rig = make(
        fb=FakeFacebook(
            open_comments_raises=StepFailed("comments"), park_raises=RuntimeError("x")
        ),
        capture=FakeCapture(start_raises=RuntimeError("cap")),
    )
    await rig.to_ready()
    await rig.c.handle(DeckPress(0))
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert ("fb.park_dashboard",) in rig.fb.calls  # later steps still ran
    assert rig.state_json["state"] == "LIVE"


async def test_dry_run_does_not_go_live(make, caplog):
    rig = make(dry_run=True)
    await rig.to_ready()
    rig.clear()
    with caplog.at_level("INFO", logger="stream_pilot.controller"):
        await rig.c.handle(DeckPress(0))
        await rig.settle()
    assert rig.c.state == State.READY
    assert rig.fb.calls == []
    assert "dry-run: would go live" in caplog.text


@pytest.mark.parametrize("state", [State.STARTING, State.LOGIN, State.SETUP, State.LIVE, State.RECONNECTING, State.ENDING, State.GOING_LIVE])
async def test_short_press_key0_ignored_outside_ready_and_error(rig, state):
    rig.c.state = state
    await rig.c.handle(DeckPress(0))
    await rig.settle()
    assert rig.c.state == state
    assert rig.fb.calls == [] and rig.obs.calls == [] and rig.capture.calls == []


# ---------------------------------------------------------------- ready / preview


async def test_ready_tick_with_preview_lost_reruns_setup_from_setup(make):
    rig = make(fb=FakeFacebook(preview_lost=[True, False]))
    await rig.to_ready()
    rig.clear()
    await rig.c.handle(Tick())
    assert rig.c.state == State.SETUP
    await rig.settle()
    assert rig.c.state == State.READY
    names = rig.names(rig.fb)
    assert "fb.logged_in" not in names and "fb.reachable" not in names
    assert "fb.setup_live" in names
    assert "obs.stop_stream" in rig.names(rig.obs)  # stale pre-roll replaced; no live exists


async def test_ready_tick_without_preview_loss_does_nothing(rig):
    await rig.to_ready()
    rig.clear()
    await rig.c.handle(Tick())
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.fb.calls == [("fb.preview_lost",)]


async def test_go_live_press_right_after_preview_lost_tick_does_not_go_live(make):
    rig = make(fb=FakeFacebook(preview_lost=[True, False]))
    await rig.to_ready()
    await rig.c.handle(Tick())
    await rig.c.handle(DeckPress(0))
    await rig.settle()
    assert "fb.go_live" not in rig.names(rig.fb)
    assert rig.c.state == State.READY


async def test_preview_lost_exception_is_ignored(make):
    rig = make(fb=FakeFacebook(preview_lost_raises=RuntimeError("cdp gone")))
    await rig.to_ready()
    await rig.c.handle(Tick())
    assert rig.c.state == State.READY


async def test_tick_rewrites_status_heartbeat(rig):
    await rig.to_ready()
    rig.status_path.unlink()
    await rig.c.handle(Tick())
    assert rig.status_json["state"] == "READY"


async def test_note_memory_updates_status(rig):
    await rig.to_ready()
    rig.c.note_memory({"available_mb": 900, "warn": True})
    assert rig.status_json["memory"] == {"available_mb": 900, "warn": True}
    await rig.c.handle(Tick())
    assert rig.status_json["memory"] == {"available_mb": 900, "warn": True}


# ---------------------------------------------------------------- power / end


async def test_power_off_while_live_exact_order(make, tmp_path):
    rig = make(shared=True, end_wait=5)
    await rig.to_live()
    stream_dir = rig.c.stream_dir
    (stream_dir / "comments.jsonl").write_text(
        json.dumps(
            {
                "captured_at": 1.0,
                "source": "fb_dom",
                "author": "Ann",
                "text": "sold",
                "live_id": "123456789",
            }
        )
        + "\n"
    )
    rig.clear()
    await rig.c.handle(PowerPress())
    assert rig.c.state == State.ENDING
    await rig.settle()
    assert rig.shared == [
        ("obs.stop_stream",),
        ("obs.stream_active",),
        ("sleep", 5),
        ("capture.stop",),
        ("fb.close_browser",),
        ("system.stop_user_unit", "obs.service"),
        ("system.poweroff",),
    ]
    report = stream_dir / "report.txt"
    assert report.read_text() == build_report(stream_dir / "comments.jsonl")
    assert rig.c.state == State.ENDING
    assert rig.state_json["state"] == "ENDING"


async def test_power_off_waits_for_stream_stop_with_timeout(make):
    rig = make(obs=FakeObs(stop_keeps_active=True))
    await rig.to_live()
    rig.sleeper.calls.clear()
    await rig.c.handle(PowerPress())
    await rig.settle()
    # stop_s = 1, polled every 0.5 s, then gives up and carries on
    assert rig.sleeper.calls[:2] == [0.5, 0.5]
    assert rig.system.calls[-1] == ("system.poweroff",)


async def test_power_off_while_live_continues_after_errors(make):
    rig = make(
        obs=FakeObs(stop_raises=RuntimeError("ws")),
        capture=FakeCapture(stop_raises=RuntimeError("cap")),
        fb=FakeFacebook(close_raises=RuntimeError("cdp")),
        system=FakeSystem(stop_unit_raises=RuntimeError("dbus")),
    )
    await rig.to_live()
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert ("system.poweroff",) in rig.system.calls
    assert ("system.stop_user_unit", "obs.service") in rig.system.calls
    assert ("fb.close_browser",) in rig.fb.calls


async def test_power_off_without_comments_file_skips_report(rig):
    await rig.to_live()
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert not (rig.c.stream_dir / "report.txt").exists()
    assert ("system.poweroff",) in rig.system.calls


async def test_power_in_reconnecting_ends_stream_and_powers_off(rig):
    await rig.to_live()
    await rig.c.handle(ObsStreamState(True, reconnecting=True))
    assert rig.c.state == State.RECONNECTING
    rig.clear()
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert rig.obs.calls[0] == ("obs.stop_stream",)
    assert rig.system.calls == [("system.stop_user_unit", "obs.service"), ("system.poweroff",)]


@pytest.mark.parametrize("state", [State.READY, State.ERROR, State.STARTING, State.GOING_LIVE])
async def test_power_in_non_live_state_closes_and_powers_off_without_stopping_stream(rig, state):
    rig.c.state = state
    await rig.c.handle(PowerPress())
    assert rig.c.state == State.ENDING
    await rig.settle()
    assert rig.fb.calls == [("fb.close_browser",)]
    assert rig.system.calls == [("system.stop_user_unit", "obs.service"), ("system.poweroff",)]
    assert "obs.stop_stream" not in rig.names(rig.obs)
    assert rig.capture.calls == []


async def test_power_during_setup_cancels_the_flow(make):
    rig = make(fb=FakeFacebook(logged_in=False))
    await rig.c.boot()
    await rig.wait_state(State.LOGIN)
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert ("system.poweroff",) in rig.system.calls
    assert rig.c.state == State.ENDING
    calls_after = len(rig.fb.calls)
    await asyncio.sleep(0.01)
    assert len(rig.fb.calls) == calls_after  # login polling is dead


async def test_power_steps_each_continue_after_failure_when_not_live(make):
    rig = make(
        fb=FakeFacebook(close_raises=RuntimeError("cdp")),
        system=FakeSystem(stop_unit_raises=RuntimeError("dbus")),
    )
    rig.c.state = State.READY
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert ("system.poweroff",) in rig.system.calls


async def test_second_power_press_ignored_while_ending(rig):
    await rig.to_live()
    await rig.c.handle(PowerPress())
    await rig.c.handle(PowerPress())  # flow has not even started yet
    await rig.settle()
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert rig.system.calls.count(("system.poweroff",)) == 1
    assert rig.obs.calls.count(("obs.stop_stream",)) == 1
    assert rig.fb.calls.count(("fb.close_browser",)) == 1


async def test_long_press_ends_stream_without_power_off_and_returns_to_setup(make):
    rig = make(shared=True)
    await rig.to_live()
    rig.clear()
    await rig.c.handle(DeckPress(0, long=True))
    assert rig.c.state == State.ENDING
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.c.live_id is None and rig.c.stream_dir is None
    shared = rig.shared
    assert shared[0] == ("obs.stop_stream",)
    assert ("capture.stop",) in shared
    assert ("fb.close_browser",) not in shared
    assert ("system.poweroff",) not in shared
    assert ("system.stop_user_unit", "obs.service") not in shared
    assert shared.count(("obs.stop_stream",)) == 1  # setup found OBS already stopped
    assert shared.index(("capture.stop",)) < shared.index(("fb.setup_live",))
    assert ("obs.start_stream",) in shared
    assert rig.store.states[-4:] == ["ENDING", "LOGIN", "SETUP", "READY"]
    assert rig.state_json == {"state": "READY", "live_id": None, "stream_dir": None}


@pytest.mark.parametrize("state", [State.READY, State.ERROR, State.LOGIN, State.ENDING, State.GOING_LIVE])
async def test_long_press_ignored_outside_live(rig, state):
    rig.c.state = state
    await rig.c.handle(DeckPress(0, long=True))
    await rig.settle()
    assert rig.c.state == state
    assert rig.fb.calls == [] and rig.obs.calls == [] and rig.capture.calls == []


# ---------------------------------------------------------------- resume


@pytest.mark.parametrize("saved_state", ["LIVE", "RECONNECTING"])
async def test_resume_while_live_never_touches_setup_or_stream(make, saved_state, tmp_path):
    rig = make(obs=FakeObs(active=True))
    stream_dir = tmp_path / "streams" / "2026-11-09_1900-42"
    rig.store.save(Saved(saved_state, "42", str(stream_dir)))
    rig.store.history.clear()
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert rig.c.live_id == "42"
    assert rig.capture.calls == [("capture.start", "42", stream_dir)]
    assert rig.names(rig.obs) == ["obs.connect", "obs.stream_active"]
    assert rig.names(rig.fb) == ["fb.connect"]
    assert "STARTING" not in rig.store.states  # live_id must survive another crash
    assert rig.state_json == {"state": "LIVE", "live_id": "42", "stream_dir": str(stream_dir)}
    assert rig.deck.faces[0] == STATUS_FACES[State.LIVE]


async def test_resume_without_stream_dir_uses_resumed_dir(make):
    rig = make(obs=FakeObs(active=True))
    rig.store.save(Saved("LIVE", "42", None))
    await rig.c.boot()
    await rig.settle()
    assert rig.capture.calls == [
        ("capture.start", "42", rig.cfg.paths.streams_dir / "resumed-42")
    ]


async def test_resume_waits_for_connections_and_is_protected_meanwhile(make, tmp_path):
    rig = make(
        obs=FakeObs(active=True, connect_raises=[ConnectionError("down"), None]),
    )
    rig.store.save(Saved("LIVE", "42", str(tmp_path / "s")))
    rig.store.history.clear()
    await rig.c.boot()
    await rig.wait_state(State.RECONNECTING)
    # a retry press while resuming must not start setup (which could stop the live)
    await rig.c.handle(DeckPress(3))
    await rig.c.handle(DeckPress(0))
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert rig.sleeper.calls == [2]
    assert "fb.setup_live" not in rig.names(rig.fb)
    assert "obs.stop_stream" not in rig.names(rig.obs)
    assert "obs.start_stream" not in rig.names(rig.obs)


async def test_resume_with_obs_not_streaming_falls_back_to_setup(make, tmp_path):
    rig = make(obs=FakeObs(active=False))
    rig.store.save(Saved("LIVE", "42", str(tmp_path / "s")))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.c.live_id is None
    assert rig.capture.calls == []
    assert "obs.stop_stream" not in rig.names(rig.obs)
    assert rig.state_json["live_id"] is None


@pytest.mark.parametrize("saved_state", ["READY", "ERROR", "SETUP", "ENDING"])
async def test_non_live_saved_state_starts_setup(make, saved_state):
    rig = make(obs=FakeObs(active=True))
    rig.store.save(Saved(saved_state, None, None))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    assert ("fb.setup_live",) in rig.fb.calls


async def test_live_saved_state_without_live_id_starts_setup(make):
    rig = make(obs=FakeObs(active=True))
    rig.store.save(Saved("LIVE", None, None))
    await rig.c.boot()
    await rig.settle()
    assert ("fb.setup_live",) in rig.fb.calls
    assert rig.capture.calls == []


# ---------------------------------------------------------------- reconnect


async def test_reconnecting_via_obs_events_never_stops_stream(rig):
    await rig.to_live()
    rig.clear()
    await rig.c.handle(ObsStreamState(active=True, reconnecting=True))
    assert rig.c.state == State.RECONNECTING
    assert rig.deck.faces[0] == STATUS_FACES[State.RECONNECTING]
    await rig.c.handle(ObsStreamState(active=True, reconnecting=False))
    assert rig.c.state == State.LIVE
    assert rig.obs.calls == []


async def test_obs_gave_up_runs_restart_loop_without_stop(make):
    rig = make()
    await rig.to_live()
    rig.clear()
    rig.obs._start = Script([RuntimeError("fail"), None])
    rig.obs.active = False
    await rig.c.handle(ObsStreamState(active=False))
    assert rig.c.state == State.RECONNECTING
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert rig.obs.calls.count(("obs.start_stream",)) == 2
    assert "obs.stop_stream" not in rig.names(rig.obs)
    assert rig.sleeper.calls[-2:] == [0.75, 0.75]


async def test_gave_up_from_reconnecting_also_restarts(make):
    rig = make()
    await rig.to_live()
    await rig.c.handle(ObsStreamState(active=True, reconnecting=True))
    rig.obs.active = False
    await rig.c.handle(ObsStreamState(active=False))
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert "obs.stop_stream" not in rig.names(rig.obs)


async def test_only_one_restart_loop_at_a_time(make):
    rig = make()
    await rig.to_live()
    rig.obs._start = Script(RuntimeError("down"))
    rig.obs.active = False
    await rig.c.handle(ObsStreamState(active=False))
    first = rig.c._restart
    await rig.c.handle(ObsStreamState(active=False))
    assert rig.c._restart is first
    await rig.c.aclose()


async def test_obs_recovery_event_cancels_restart_loop(make):
    rig = make()
    await rig.to_live()
    rig.obs._start = Script(RuntimeError("down"))
    rig.obs.active = False
    await rig.c.handle(ObsStreamState(active=False))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ("obs.start_stream",) in rig.obs.calls
    await rig.c.handle(ObsStreamState(active=True, reconnecting=False))
    assert rig.c.state == State.LIVE
    await rig.settle()  # restart task is gone, so this returns
    count = rig.obs.calls.count(("obs.start_stream",))
    await asyncio.sleep(0.01)
    assert rig.obs.calls.count(("obs.start_stream",)) == count
    assert "obs.stop_stream" not in rig.names(rig.obs)


@pytest.mark.parametrize(
    "event",
    [
        ObsStreamState(active=False),
        ObsStreamState(active=True, reconnecting=True),
        ObsStreamState(active=True),
    ],
)
@pytest.mark.parametrize("state", [State.READY, State.ENDING, State.SETUP, State.ERROR])
async def test_obs_events_ignored_outside_live_states(rig, state, event):
    rig.c.state = state
    await rig.c.handle(event)
    await rig.settle()
    assert rig.c.state == state
    assert rig.obs.calls == []


async def test_live_tick_with_inactive_stream_restarts_loop_without_stop(make):
    rig = make()
    await rig.to_live()
    rig.clear()
    rig.obs.active = False  # OBS crashed and was restarted; no event arrived
    await rig.c.handle(Tick())
    assert rig.c.state == State.RECONNECTING
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert ("obs.start_stream",) in rig.obs.calls
    assert "obs.stop_stream" not in rig.names(rig.obs)


async def test_live_tick_with_active_stream_does_nothing(rig):
    await rig.to_live()
    rig.clear()
    await rig.c.handle(Tick())
    assert rig.c.state == State.LIVE
    assert rig.obs.calls == [("obs.stream_active",)]


async def test_live_tick_poll_error_is_ignored(make):
    rig = make()
    await rig.to_live()
    rig.clear()
    rig.obs.active = False
    rig.obs._active = Script(RuntimeError("ws"))  # polling now raises
    await rig.c.handle(Tick())
    assert rig.c.state == State.LIVE
    assert rig.obs.calls == [("obs.stream_active",)]  # no restart, no stop


# ---------------------------------------------------------------- deck cameras


async def test_main_camera_key(rig):
    await rig.c.boot()
    await rig.c.handle(DeckPress(2))
    assert ("obs.set_scene", "Main Camera") in rig.obs.calls
    assert rig.camera.calls == [("camera.set_autofocus", "/dev/video0", True)]
    assert rig.deck.faces[1] == KeyFace("AF\nON", "#1e9e3e")
    assert rig.deck.faces[2].bg == "#1f5fbf"
    assert rig.deck.faces[5].bg == "#333333"


async def test_jewelry_camera_key_and_scene_order(rig):
    await rig.c.boot()
    rig.obs.calls.clear()
    await rig.c.handle(DeckPress(5))
    assert rig.obs.calls == [("obs.set_scene", "Jewlery Camera")]
    assert rig.camera.calls == [("camera.set_autofocus", "/dev/video2", True)]
    assert rig.deck.faces[5].bg == "#1f5fbf"
    assert rig.deck.faces[2].bg == "#333333"


async def test_autofocus_key_toggles_on_active_camera(rig):
    await rig.c.boot()
    await rig.c.handle(DeckPress(5))
    await rig.c.handle(DeckPress(1))
    assert rig.camera.calls[-1] == ("camera.set_autofocus", "/dev/video2", False)
    assert rig.deck.faces[1] == KeyFace("AF\nOFF", "#555555")
    await rig.c.handle(DeckPress(1))
    assert rig.camera.calls[-1] == ("camera.set_autofocus", "/dev/video2", True)
    assert rig.deck.faces[1] == KeyFace("AF\nON", "#1e9e3e")


async def test_autofocus_key_without_active_camera_uses_current_scene(make):
    rig = make(obs=FakeObs(scene="Jewlery Camera"))
    await rig.c.boot()
    await rig.c.handle(DeckPress(1))
    assert rig.camera.calls == [("camera.set_autofocus", "/dev/video2", True)]
    assert rig.deck.faces[1].label == "AF\nON"
    assert rig.deck.faces[5].bg == "#1f5fbf"


async def test_autofocus_key_unknown_scene_falls_back_to_main(make):
    rig = make(obs=FakeObs(scene="Black"))
    await rig.c.boot()
    await rig.c.handle(DeckPress(1))
    assert rig.camera.calls == [("camera.set_autofocus", "/dev/video0", True)]


async def test_camera_errors_are_logged_not_raised(make):
    rig = make(camera=FakeCamera(raises=RuntimeError("v4l2")))
    await rig.c.boot()
    await rig.c.handle(DeckPress(2))
    await rig.c.handle(DeckPress(1))
    assert rig.deck.faces[1].label == "AF\nOFF"


async def test_scene_failure_skips_autofocus(make):
    rig = make(obs=FakeObs(set_scene_raises=RuntimeError("ws")))
    await rig.c.boot()
    await rig.c.handle(DeckPress(2))
    assert rig.camera.calls == []
    assert rig.deck.faces[2].bg == "#333333"


async def test_camera_keys_work_while_live_without_changing_state(rig):
    await rig.to_live()
    await rig.c.handle(DeckPress(5))
    assert rig.c.state == State.LIVE
    assert ("obs.set_scene", "Jewlery Camera") in rig.obs.calls
    assert "obs.stop_stream" not in rig.names(rig.obs)


async def test_unknown_key_ignored(rig):
    await rig.to_ready()
    rig.clear()
    await rig.c.handle(DeckPress(4))
    assert rig.obs.calls == [] and rig.fb.calls == []


# ---------------------------------------------------------------- threading / run


async def test_sink_from_another_thread_enqueues_and_run_processes(rig):
    runner = asyncio.create_task(rig.c.run())
    try:
        await asyncio.wait_for(rig.c.wait_idle(), 2)
        await rig.wait_state(State.READY)
        t = threading.Thread(target=rig.c.sink, args=(DeckPress(2),))
        t.start()
        t.join()
        for _ in range(2000):
            if rig.camera.calls:
                break
            await asyncio.sleep(0.001)
        assert rig.camera.calls == [("camera.set_autofocus", "/dev/video0", True)]
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def test_sink_on_loop_thread_enqueues(rig):
    runner = asyncio.create_task(rig.c.run())
    try:
        await rig.wait_state(State.READY)
        rig.c.sink(DeckPress(5))
        for _ in range(2000):
            if rig.camera.calls:
                break
            await asyncio.sleep(0)
        assert rig.camera.calls == [("camera.set_autofocus", "/dev/video2", True)]
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def test_handle_swallows_unexpected_errors(rig, monkeypatch):
    await rig.to_ready()

    async def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(rig.c, "_camera_press", boom)
    await rig.c.handle(DeckPress(2))  # must not raise
    assert rig.c.state == State.READY


def test_live_states_constant_matches_controller_expectation():
    assert LIVE_STATES == {State.LIVE, State.RECONNECTING}
