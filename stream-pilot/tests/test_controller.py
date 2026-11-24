import asyncio
import json
import re
import threading
from pathlib import Path

import pytest

from stream_pilot import controller as controller_mod
from stream_pilot.comment_log import build_report
from stream_pilot.config import load_config
from stream_pilot.controller import Controller
from stream_pilot.events import (
    DeckPress,
    ObsStreamState,
    PowerPress,
    RetryRequested,
    SsnStalled,
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
        clock=None,
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
            **({"clock": clock} if clock is not None else {}),
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

    async def wait_call(self, obj, name: str) -> None:
        for _ in range(2000):
            if name in self.names(obj):
                return
            await asyncio.sleep(0)
        raise AssertionError(f"{name} never called")

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
        ("obs.current_scene",),
        ("fb.connect",),
        ("fb.reachable",),
        ("fb.refresh_ssn",),
        ("fb.logged_in",),
        ("obs.stream_active",),
        ("fb.setup_live",),
        ("obs.set_stream_target", rig.fb.target),
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


async def test_ssn_background_refreshed_once_network_is_confirmed(make):
    # SSN creates its background tab as Chromium starts, which can be before the
    # network is up; it is only reopened once facebook.com answers.
    rig = make(fb=FakeFacebook(reachable=[False, False, True]))
    await rig.c.boot()
    await rig.settle()
    names = rig.names(rig.fb)
    assert names.count("fb.refresh_ssn") == 1
    last_reachable = len(names) - 1 - names[::-1].index("fb.reachable")
    assert last_reachable < names.index("fb.refresh_ssn") < names.index("fb.logged_in")


async def test_ssn_refresh_failure_does_not_block_setup(make, caplog):
    rig = make(fb=FakeFacebook(refresh_ssn_raises=[RuntimeError("Target closed")]))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    assert "Target closed" in caplog.text


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
    assert names.index("obs.stop_stream") < names.index("obs.set_stream_target")
    assert names.index("obs.set_stream_target") < names.index("obs.start_stream")


async def test_setup_waits_out_facebooks_reconnect_window_after_stopping_a_pre_roll(make):
    rig = make(shared=True, obs=None, clock=lambda: 100.0)
    rig.obs.active = True
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    calls = [c[0] for c in rig.shared]
    # the stop comes before Facebook's setup steps, so they count toward the wait
    assert calls.index("obs.stop_stream") < calls.index("fb.setup_live")
    wait = ("sleep", controller_mod.PREROLL_RECONNECT_WAIT_S)
    assert rig.shared.index(wait) < calls.index("obs.start_stream")
    assert controller_mod.PREROLL_RECONNECT_WAIT_S >= 60  # measured on the stream PC: ~60 s


async def test_setup_after_an_obs_stop_event_waits_only_the_rest_of_the_window(make):
    now = [100.0]
    rig = make(clock=lambda: now[0])
    await rig.c.handle(ObsStreamState(active=False))
    now[0] = 130.0
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    assert controller_mod.PREROLL_RECONNECT_WAIT_S - 30 in rig.sleeper.calls


async def test_setup_long_after_the_last_stop_does_not_wait(make):
    now = [100.0]
    rig = make(clock=lambda: now[0])
    await rig.c.handle(ObsStreamState(active=False))
    now[0] = 100.0 + controller_mod.PREROLL_RECONNECT_WAIT_S + 1
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.sleeper.calls == []


async def test_preview_wait_restarts_a_pre_roll_that_facebook_dropped(make, monkeypatch):
    monkeypatch.setattr(controller_mod, "PREROLL_CHECK_S", 0.01)
    rig = make()
    rig.fb.gates["wait_preview_ready"] = gate = asyncio.Event()
    starts = []

    def start_fails_to_connect_once():
        starts.append(1)
        if len(starts) == 1:
            rig.obs.active = False  # OBS accepted StartStream, then the RTMP connect failed

    rig.obs.hooks["start_stream_done"] = start_fails_to_connect_once
    await rig.c.boot()
    for _ in range(200):
        if len(starts) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(starts) == 2
    assert rig.c.state == State.SETUP
    gate.set()
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.obs.active


async def test_preview_wait_leaves_a_running_pre_roll_alone(make, monkeypatch):
    monkeypatch.setattr(controller_mod, "PREROLL_CHECK_S", 0.01)
    rig = make()
    rig.fb.gates["wait_preview_ready"] = gate = asyncio.Event()
    await rig.c.boot()
    await asyncio.sleep(0.1)
    gate.set()
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.names(rig.obs).count("obs.start_stream") == 1


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
    assert names[:3] == ["fb.reachable", "fb.refresh_ssn", "fb.logged_in"]
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
    assert "fb.refresh_ssn" not in names  # SSN is only refreshed before LOGIN
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
    # current_scene is the read-only camera-key sync; nothing touches the stream
    assert rig.names(rig.obs) == ["obs.connect", "obs.current_scene", "obs.stream_active"]
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


async def test_connect_shows_live_camera_and_its_real_autofocus(make):
    rig = make(
        obs=FakeObs(scene="Jewlery Camera"),
        camera=FakeCamera(autofocus={"/dev/video2": True}),
    )
    await rig.to_ready()
    assert ("camera.get_autofocus", "/dev/video2") in rig.camera.calls
    assert rig.deck.faces[1] == KeyFace("AF\nON", "#1e9e3e")
    assert rig.deck.faces[5].bg == "#1f5fbf"
    assert rig.deck.faces[2].bg == "#333333"


async def test_first_autofocus_press_toggles_the_real_state(make):
    rig = make(camera=FakeCamera(autofocus={"/dev/video0": True}))
    await rig.to_ready()
    await rig.c.handle(DeckPress(1))
    assert rig.camera.calls[-1] == ("camera.set_autofocus", "/dev/video0", False)
    assert rig.deck.faces[1] == KeyFace("AF\nOFF", "#555555")


async def test_connect_with_non_camera_scene_marks_no_camera(make):
    rig = make(obs=FakeObs(scene="Black"), camera=FakeCamera(autofocus={"/dev/video0": True}))
    await rig.to_ready()
    assert rig.camera.calls == []
    assert rig.deck.faces[1].label == "AF\nOFF"
    assert rig.deck.faces[2].bg == "#333333" and rig.deck.faces[5].bg == "#333333"


async def test_unreadable_autofocus_keeps_assumed_off(make):
    rig = make(camera=FakeCamera(autofocus={}))
    await rig.to_ready()
    assert rig.deck.faces[2].bg == "#1f5fbf"
    assert rig.deck.faces[1].label == "AF\nOFF"
    await rig.c.handle(DeckPress(1))
    assert rig.camera.calls[-1] == ("camera.set_autofocus", "/dev/video0", True)


async def test_camera_errors_are_logged_not_raised(make):
    rig = make(camera=FakeCamera(raises=RuntimeError("v4l2")))
    await rig.c.boot()
    await rig.c.handle(DeckPress(2))
    await rig.c.handle(DeckPress(1))
    assert rig.deck.faces[1].label == "AF\nOFF"


async def test_scene_failure_skips_autofocus(make):
    rig = make(obs=FakeObs(set_scene_raises=RuntimeError("ws")))
    await rig.c.boot()
    await rig.c.handle(DeckPress(5))
    assert rig.camera.calls == []
    assert rig.deck.faces[5].bg == "#333333"


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


# ------------------------------------------------- fix round 1: post-go-live


async def test_live_is_persisted_before_open_comments(rig):
    seen = []
    rig.fb.hooks["open_comments"] = lambda: seen.append(rig.state_json)
    await rig.to_ready()
    await rig.c.handle(DeckPress(0))
    await rig.settle()
    assert len(seen) == 1
    assert seen[0] == {
        "state": "LIVE",
        "live_id": "123456789",
        "stream_dir": str(rig.c.stream_dir),
    }
    assert rig.status_json["state"] == "LIVE"
    assert rig.deck.faces[0] == STATUS_FACES[State.LIVE]


async def _live_with_hung_open_comments(rig):
    await rig.to_ready()
    rig.fb.gates["open_comments"] = asyncio.Event()  # never set: slow adapter
    await rig.c.handle(DeckPress(0))
    await rig.wait_call(rig.fb, "fb.open_comments")
    assert rig.c.state == State.LIVE
    assert "capture.start" not in rig.names(rig.capture)


async def test_power_during_slow_open_comments_runs_normal_end_flow(make):
    rig = make(shared=True)
    await _live_with_hung_open_comments(rig)
    stream_dir = rig.c.stream_dir
    (stream_dir / "comments.jsonl").write_text(
        json.dumps(
            {"captured_at": 1.0, "source": "fb_dom", "author": "Ann", "text": "x", "live_id": "123456789"}
        )
        + "\n"
    )
    rig.clear()
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert [c for c in rig.shared if c[0] != "sleep"] == [
        ("obs.stop_stream",),
        ("obs.stream_active",),
        ("capture.stop",),
        ("fb.close_browser",),
        ("system.stop_user_unit", "obs.service"),
        ("system.poweroff",),
    ]
    assert (stream_dir / "report.txt").read_text() == build_report(stream_dir / "comments.jsonl")
    assert "fb.park_dashboard" not in rig.names(rig.fb)  # half-done steps cancelled


async def test_long_press_during_slow_open_comments_ends_without_power_off(rig):
    await _live_with_hung_open_comments(rig)
    rig.clear()
    rig.fb.gates.clear()
    await rig.c.handle(DeckPress(0, long=True))
    await rig.settle()
    assert rig.c.state == State.READY
    assert rig.obs.calls[0] == ("obs.stop_stream",)
    assert ("system.poweroff",) not in rig.system.calls


async def test_obs_events_during_post_go_live_steps_follow_live_rules(rig):
    await _live_with_hung_open_comments(rig)
    await rig.c.handle(ObsStreamState(active=True, reconnecting=True))
    assert rig.c.state == State.RECONNECTING
    await rig.c.handle(ObsStreamState(active=True, reconnecting=False))
    assert rig.c.state == State.LIVE
    assert "obs.stop_stream" not in rig.names(rig.obs)


async def test_post_go_live_steps_run_in_live_after_open_comments_released(rig):
    await _live_with_hung_open_comments(rig)
    rig.fb.gates["open_comments"].set()
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert rig.capture.calls[0][0] == "capture.start"
    assert ("fb.park_dashboard",) in rig.fb.calls


# ------------------------------------------- fix round 1: power during long-press end


async def test_power_press_during_long_press_end_powers_off_instead_of_setup(rig):
    await rig.to_live()
    rig.clear()
    await rig.c.handle(DeckPress(0, long=True))
    assert rig.c.state == State.ENDING
    await rig.c.handle(PowerPress())  # used to be dropped
    await rig.settle()
    assert rig.system.calls == [("system.stop_user_unit", "obs.service"), ("system.poweroff",)]
    assert rig.fb.calls == [("fb.close_browser",)]  # no setup flow started
    assert rig.obs.calls.count(("obs.stop_stream",)) == 1
    assert rig.c.state == State.ENDING


async def test_power_press_after_long_press_end_branch_point_shuts_down(make):
    rig = make()
    await rig.to_live()
    rig.clear()
    rig.fb.gates["reachable"] = asyncio.Event()  # setup stalls right after the branch point
    await rig.c.handle(DeckPress(0, long=True))
    await rig.wait_call(rig.fb, "fb.reachable")
    assert rig.c.state == State.ENDING
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert rig.system.calls == [("system.stop_user_unit", "obs.service"), ("system.poweroff",)]
    assert rig.fb.calls.count(("fb.close_browser",)) == 1
    assert "fb.setup_live" not in rig.names(rig.fb)


async def test_two_power_presses_during_long_press_end_power_off_once(rig):
    await rig.to_live()
    rig.clear()
    await rig.c.handle(DeckPress(0, long=True))
    await rig.c.handle(PowerPress())
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert rig.system.calls.count(("system.poweroff",)) == 1


# --------------------------------------------------- fix round 1: bounded inline awaits


@pytest.fixture
def short_inline_timeout(monkeypatch):
    monkeypatch.setattr(controller_mod, "INLINE_TIMEOUT_S", 0.05)


async def test_hung_preview_lost_does_not_block_handle(rig, short_inline_timeout, caplog):
    await rig.to_ready()
    rig.fb.gates["preview_lost"] = asyncio.Event()
    await asyncio.wait_for(rig.c.handle(Tick()), 2)
    assert rig.c.state == State.READY
    assert "timed out" in caplog.text
    # a queued power press is handled afterwards
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert ("system.poweroff",) in rig.system.calls


async def test_hung_live_poll_means_no_action(rig, short_inline_timeout):
    await rig.to_live()
    rig.clear()
    rig.obs.gates["stream_active"] = asyncio.Event()
    await asyncio.wait_for(rig.c.handle(Tick()), 2)
    assert rig.c.state == State.LIVE
    assert rig.obs.calls == [("obs.stream_active",)]


async def test_hung_set_scene_does_not_block_handle(rig, short_inline_timeout):
    await rig.c.boot()
    rig.obs.gates["set_scene"] = asyncio.Event()
    await asyncio.wait_for(rig.c.handle(DeckPress(5)), 2)
    assert rig.camera.calls == []
    assert rig.deck.faces[5].bg == "#333333"


async def test_hung_autofocus_does_not_block_handle(rig, short_inline_timeout):
    await rig.c.boot()
    rig.camera.gates["set_autofocus"] = asyncio.Event()
    await asyncio.wait_for(rig.c.handle(DeckPress(2)), 2)
    await asyncio.wait_for(rig.c.handle(DeckPress(1)), 2)
    assert rig.deck.faces[1].label == "AF\nOFF"


async def test_hung_current_scene_falls_back_to_main(rig, short_inline_timeout):
    await rig.c.boot()
    rig.obs.gates["current_scene"] = asyncio.Event()
    await asyncio.wait_for(rig.c.handle(DeckPress(1)), 2)
    assert rig.camera.calls == [("camera.set_autofocus", "/dev/video0", True)]


# ------------------------------------------------ fix round 1: setup never stops a live


async def test_setup_phase_refuses_stop_stream_while_live(rig):
    await rig.c.boot()
    await rig.settle()
    rig.clear()

    async def active_but_state_flipped_to_live():
        rig.obs.calls.append(("obs.stream_active",))
        rig.c.state = State.LIVE  # simulate a concurrent change
        return True

    rig.obs.stream_active = active_but_state_flipped_to_live
    assert await rig.c._setup_phase() is False  # bails out; never raises/errors a live
    assert rig.c.state == State.LIVE
    assert "obs.stop_stream" not in rig.names(rig.obs)
    assert "obs.set_stream_target" not in rig.names(rig.obs)
    assert "obs.start_stream" not in rig.names(rig.obs)


# ------------------------------------ hardening: bounded end flow, setup live guard


@pytest.fixture
def short_end_timeout(monkeypatch):
    monkeypatch.setattr(controller_mod, "END_STEP_TIMEOUT_S", 0.05)


def test_end_step_timeout_default_is_15_seconds():
    assert controller_mod.END_STEP_TIMEOUT_S == 15


async def test_hung_close_browser_still_powers_off(rig, short_end_timeout, caplog):
    await rig.to_live()
    rig.fb.gates["close_browser"] = asyncio.Event()  # never set: hung
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert rig.names(rig.fb).count("fb.close_browser") == 1
    assert ("system.stop_user_unit", "obs.service") in rig.system.calls
    assert rig.system.calls.count(("system.poweroff",)) == 1
    assert "close_browser timed out" in caplog.text


async def test_hung_close_browser_in_non_live_shutdown_still_powers_off(
    rig, short_end_timeout
):
    await rig.to_ready()
    rig.fb.gates["close_browser"] = asyncio.Event()
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert rig.system.calls.count(("system.poweroff",)) == 1


async def test_hung_stop_user_unit_still_powers_off(rig, short_end_timeout):
    await rig.to_ready()
    rig.system.gates["stop_user_unit"] = asyncio.Event()
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert rig.system.calls.count(("system.poweroff",)) == 1


async def test_hung_stop_stream_and_capture_stop_still_power_off(rig, short_end_timeout):
    await rig.to_live()
    rig.obs.gates["stop_stream"] = asyncio.Event()
    rig.capture.gates["stop"] = asyncio.Event()
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert ("obs.stop_stream",) in rig.obs.calls
    assert ("capture.stop",) in rig.capture.calls
    assert rig.system.calls.count(("system.poweroff",)) == 1


async def test_hung_stream_active_poll_in_end_flow_still_powers_off(
    rig, short_end_timeout
):
    await rig.to_live()
    rig.obs.gates["stream_active"] = asyncio.Event()
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert rig.system.calls.count(("system.poweroff",)) == 1


async def test_long_press_end_with_hung_capture_stop_returns_to_setup(
    rig, short_end_timeout
):
    await rig.to_live()
    rig.capture.gates["stop"] = asyncio.Event()
    await rig.c.handle(DeckPress(0, long=True))
    await rig.settle()
    assert rig.c.state == State.READY
    assert ("system.poweroff",) not in rig.system.calls


async def test_setup_phase_returns_without_touching_obs_or_state_when_live(rig, caplog):
    await rig.to_live()
    rig.clear()
    saves = len(rig.store.history)
    await rig.c._setup_phase()  # must not raise
    assert rig.c.state == State.LIVE
    assert rig.obs.calls == []
    assert rig.fb.calls == []
    assert len(rig.store.history) == saves
    assert "setup" in caplog.text and "LIVE" in caplog.text


async def test_setup_attempt_guard_checks_state_on_every_attempt(rig):
    """A live that appears between attempts must not be touched by a retry."""
    await rig.c.boot()
    await rig.settle()
    rig.clear()

    original = rig.fb.setup_live

    async def fail_then_go_live():
        await original()
        rig.c.state = State.LIVE  # simulate a concurrent change
        raise StepFailed("setup", "boom")

    rig.fb.setup_live = fail_then_go_live
    await rig.c._setup_phase()  # attempt 1 fails; attempt 2 sees LIVE and bails
    assert rig.c.state == State.LIVE
    assert rig.names(rig.fb).count("fb.setup_live") == 1
    # attempt 1 replaced the pre-roll before the live appeared; the retry touched nothing
    assert rig.names(rig.obs).count("obs.stop_stream") == 1
    assert "obs.start_stream" not in rig.names(rig.obs)


async def test_setup_flow_never_marks_a_live_ready(rig):
    await rig.to_live()
    rig.clear()
    await rig.c._setup_flow("SETUP")
    assert rig.c.state == State.LIVE
    assert rig.obs.calls == []


def test_live_states_constant_matches_controller_expectation():
    assert LIVE_STATES == {State.LIVE, State.RECONNECTING}


# ------------------------------------------------ capture re-attach on Tick (plan 3 task 6)


class HealthCapture(FakeCapture):
    """FakeCapture with the optional ``healthy()`` probe the adapter provides."""

    def __init__(self, *, healthy: bool = True, health_raises=None, **kw):
        super().__init__(**kw)
        self.is_healthy = healthy
        self.health_raises = health_raises

    async def start(self, live_id, stream_dir) -> None:
        await super().start(live_id, stream_dir)
        try:
            await self._gated("start")  # FakeCapture itself has no start gate
        except asyncio.CancelledError:
            self.calls.append(("capture.start.cancelled",))
            raise

    def healthy(self) -> bool:
        self.calls.append(("capture.healthy",))
        if self.health_raises is not None:
            raise self.health_raises
        return self.is_healthy


def _starts(rig):
    return [c for c in rig.capture.calls if c[0] == "capture.start"]


async def test_live_tick_reattaches_unhealthy_capture(make):
    cap = HealthCapture(healthy=True)
    rig = make(capture=cap)
    await rig.to_live()
    stream_dir = rig.c.stream_dir
    live_id = rig.c.live_id
    cap.calls.clear()
    cap.is_healthy = False  # popout closed (Chromium crash/restart)
    await rig.c.handle(Tick())
    await rig.settle()
    assert _starts(rig) == [("capture.start", live_id, stream_dir)]
    assert rig.c.state == State.LIVE


async def test_live_tick_does_not_reattach_healthy_capture(make):
    cap = HealthCapture(healthy=True)
    rig = make(capture=cap)
    await rig.to_live()
    cap.calls.clear()
    await rig.c.handle(Tick())
    await rig.settle()
    assert _starts(rig) == []
    assert cap.calls == [("capture.healthy",)]


async def test_live_tick_without_healthy_probe_does_not_reattach(rig):
    await rig.to_live()
    rig.capture.calls.clear()
    await rig.c.handle(Tick())
    await rig.settle()
    assert rig.capture.calls == []


async def test_reconnecting_tick_also_reattaches_capture(make):
    cap = HealthCapture(healthy=True)
    rig = make(capture=cap)
    await rig.to_live()
    rig.c.state = State.RECONNECTING
    cap.calls.clear()
    cap.is_healthy = False
    await rig.c.handle(Tick())
    await rig.settle()
    assert len(_starts(rig)) == 1


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_reattach_failure_never_reaches_the_controller(make, caplog):
    cap = HealthCapture(healthy=True)
    rig = make(capture=cap)
    clock = FakeClock()
    rig.c.clock = clock
    await rig.to_live()
    cap.calls.clear()
    cap.is_healthy = False
    cap._start = Script([RuntimeError("no popout"), RuntimeError("still none")])
    await rig.c.handle(Tick())
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert "capture.reattach failed" in caplog.text
    clock.t += 30.0  # past the first backoff
    await rig.c.handle(Tick())  # retried
    await rig.settle()
    assert len(_starts(rig)) == 2
    assert rig.c.state == State.LIVE


async def test_health_probe_error_is_ignored(make):
    cap = HealthCapture(healthy=True, health_raises=RuntimeError("probe"))
    rig = make(capture=cap)
    await rig.to_live()
    cap.calls.clear()
    await rig.c.handle(Tick())
    await rig.settle()
    assert _starts(rig) == []
    assert rig.c.state == State.LIVE


async def test_only_one_reattach_runs_at_a_time(make):
    cap = HealthCapture(healthy=True)
    rig = make(capture=cap)
    await rig.to_live()
    cap.calls.clear()
    cap.is_healthy = False
    cap.gates["start"] = asyncio.Event()  # reattach hangs
    await rig.c.handle(Tick())
    await rig.wait_call(cap, "capture.start")
    await rig.c.handle(Tick())
    await rig.c.handle(Tick())
    assert len(_starts(rig)) == 1
    await rig.c.aclose()  # cancels the hung re-attach


async def test_end_flow_cancels_a_pending_reattach(make):
    cap = HealthCapture(healthy=True)
    rig = make(capture=cap)
    await rig.to_live()
    cap.calls.clear()
    cap.is_healthy = False
    cap.gates["start"] = asyncio.Event()
    await rig.c.handle(Tick())
    await rig.wait_call(cap, "capture.start")
    await rig.c.handle(DeckPress(0, long=True))
    await rig.settle()
    assert ("capture.stop",) in cap.calls
    assert rig.c._reattach is None or rig.c._reattach.done()
    # the pending start was cancelled (not completed) before capture.stop ran
    assert ("capture.start.cancelled",) in cap.calls
    assert cap.calls.index(("capture.start.cancelled",)) < cap.calls.index(("capture.stop",))


async def test_ready_tick_never_probes_capture(make):
    cap = HealthCapture(healthy=False)
    rig = make(capture=cap)
    await rig.to_ready()
    cap.calls.clear()
    await rig.c.handle(Tick())
    await rig.settle()
    assert cap.calls == []


async def test_ending_tick_never_reattaches(make):
    cap = HealthCapture(healthy=True)
    rig = make(capture=cap)
    await rig.to_live()
    cap.is_healthy = False
    rig.obs.gates["stop_stream"] = asyncio.Event()  # the end flow parks in ENDING
    await rig.c.handle(DeckPress(0, long=True))
    await rig.wait_state(State.ENDING)
    cap.calls.clear()
    await rig.c.handle(Tick())
    assert cap.calls == []
    await rig.c.aclose()


# ---------------------------------------- bounded cancel of a stuck re-attach (review I1)


class StubbornCapture(HealthCapture):
    """start() swallows the first cancel and then sleeps (a hung adapter cleanup)."""

    hang = False  # set by the test once the stream is live

    async def start(self, live_id, stream_dir) -> None:
        self.calls.append(("capture.start", live_id, stream_dir))
        if not self.hang:
            return
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.calls.append(("capture.start.cancelled",))
            await asyncio.sleep(60)  # ignores the cancel; only a second cancel ends it


async def _poll(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


async def _kill(task):
    for _ in range(3):
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.wait({task}, timeout=0.5)


@pytest.fixture
def short_cancel_wait(monkeypatch):
    monkeypatch.setattr(controller_mod, "REATTACH_CANCEL_WAIT_S", 0.2)


async def test_end_flow_is_not_blocked_by_a_stuck_reattach(make, short_cancel_wait, caplog):
    cap = StubbornCapture(healthy=True)
    rig = make(capture=cap)
    await rig.to_live()
    cap.hang = True
    cap.calls.clear()
    cap.is_healthy = False
    await rig.c.handle(Tick())
    await rig.wait_call(cap, "capture.start")
    task = rig.c._reattach
    await asyncio.wait_for(rig.c.handle(DeckPress(0, long=True)), 2)  # must not hang
    await _poll(lambda: ("obs.stop_stream",) in rig.obs.calls)
    assert "abandon" in caplog.text
    await _kill(task)
    await rig.c.aclose()


async def test_power_press_is_not_blocked_by_a_stuck_reattach(make, short_cancel_wait):
    cap = StubbornCapture(healthy=True)
    rig = make(capture=cap)
    await rig.to_live()
    cap.hang = True
    cap.calls.clear()
    cap.is_healthy = False
    await rig.c.handle(Tick())
    await rig.wait_call(cap, "capture.start")
    task = rig.c._reattach
    await asyncio.wait_for(rig.c.handle(PowerPress()), 2)
    await _poll(lambda: ("obs.stop_stream",) in rig.obs.calls)
    await _kill(task)
    await rig.c.aclose()


async def test_aclose_is_not_blocked_by_a_stuck_reattach(make, short_cancel_wait):
    cap = StubbornCapture(healthy=True)
    rig = make(capture=cap)
    await rig.to_live()
    cap.hang = True
    cap.calls.clear()
    cap.is_healthy = False
    await rig.c.handle(Tick())
    await rig.wait_call(cap, "capture.start")
    task = rig.c._reattach
    await asyncio.wait_for(rig.c.aclose(), 2)
    await _kill(task)


# ----------------------------------------------------- re-attach backoff (review I3)


async def test_reattach_backs_off_exponentially_and_resets_on_success(make):
    cap = HealthCapture(healthy=True)
    rig = make(capture=cap)
    clock = FakeClock()
    rig.c.clock = clock
    await rig.to_live()
    cap.calls.clear()
    cap.is_healthy = False
    cap._start = Script([RuntimeError("1"), RuntimeError("2"), RuntimeError("3"), None])

    async def tick():
        await rig.c.handle(Tick())
        await rig.settle()

    await tick()  # failure 1 -> wait 30 s
    assert len(_starts(rig)) == 1
    clock.t += 29
    await tick()
    assert len(_starts(rig)) == 1  # still backing off
    clock.t += 1
    await tick()  # failure 2 -> wait 60 s
    assert len(_starts(rig)) == 2
    clock.t += 59
    await tick()
    assert len(_starts(rig)) == 2
    clock.t += 1
    await tick()  # failure 3 -> wait 120 s
    assert len(_starts(rig)) == 3
    clock.t += 119
    await tick()
    assert len(_starts(rig)) == 3
    clock.t += 1
    await tick()  # success: backoff resets
    assert len(_starts(rig)) == 4
    # later the popout dies again: the very next Tick re-attaches (no backoff left)
    await tick()
    assert len(_starts(rig)) == 5


async def test_reattach_backoff_is_capped_at_300_seconds(make):
    cap = HealthCapture(healthy=True)
    rig = make(capture=cap)
    clock = FakeClock()
    rig.c.clock = clock
    await rig.to_live()
    cap.is_healthy = False
    cap._start = Script(RuntimeError("down"))  # always fails
    for _ in range(12):
        clock.t += 1000
        await rig.c.handle(Tick())
        await rig.settle()
    cap.calls.clear()
    clock.t += 299
    await rig.c.handle(Tick())
    await rig.settle()
    assert _starts(rig) == []
    clock.t += 1
    await rig.c.handle(Tick())
    await rig.settle()
    assert len(_starts(rig)) == 1


# ------------------------------------------ bounded go-live / resume capture steps (review m6)


async def test_hung_open_comments_and_capture_start_do_not_stall_go_live(make, monkeypatch):
    monkeypatch.setattr(controller_mod, "CAPTURE_STEP_TIMEOUT_S", 0.05)
    cap = HealthCapture(healthy=True)
    rig = make(capture=cap)
    rig.fb.gates["open_comments"] = asyncio.Event()
    cap.gates["start"] = asyncio.Event()
    await rig.to_ready()
    await rig.c.handle(DeckPress(0))
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert ("fb.park_dashboard",) in rig.fb.calls
    assert ("capture.start.cancelled",) in cap.calls


async def test_hung_capture_start_does_not_stall_resume(make, monkeypatch, tmp_path):
    monkeypatch.setattr(controller_mod, "CAPTURE_STEP_TIMEOUT_S", 0.05)
    cap = HealthCapture(healthy=True)
    cap.gates["start"] = asyncio.Event()
    rig = make(obs=FakeObs(active=True), capture=cap)
    rig.store.save(Saved("LIVE", "42", str(tmp_path / "s")))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.LIVE


# ------------------------------------------- backoff state is per stream (re-review N2)


async def test_reattach_backoff_resets_for_a_new_stream(make):
    cap = HealthCapture(healthy=True)
    rig = make(capture=cap)
    clock = FakeClock()
    rig.c.clock = clock
    await rig.to_live()
    cap.is_healthy = False
    cap._start = Script(RuntimeError("down"))
    for _ in range(3):  # three failures: the backoff is now 240 s
        await rig.c.handle(Tick())
        await rig.settle()
        clock.t += 1000
        clock.t -= 995  # stay inside the current backoff window
    assert rig.c._reattach_failures >= 1
    # the stream ends (long press) and a new one goes live
    await rig.c.handle(DeckPress(0, long=True))
    await rig.settle()
    assert rig.c._reattach_failures == 0 and rig.c._reattach_next_try == 0.0
    cap._start = Script(None)
    cap.is_healthy = True
    await rig.c.handle(DeckPress(0))
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert rig.c._reattach_failures == 0 and rig.c._reattach_next_try == 0.0
    cap.calls.clear()
    cap.is_healthy = False
    await rig.c.handle(Tick())  # no leftover backoff: re-attaches at once
    await rig.settle()
    assert len(_starts(rig)) == 1


async def test_reattach_backoff_resets_on_resume(make, tmp_path):
    rig = make(obs=FakeObs(active=True))
    rig.c._reattach_failures = 5
    rig.c._reattach_next_try = 1e12
    rig.store.save(Saved("LIVE", "42", str(tmp_path / "s")))
    await rig.c.boot()
    await rig.settle()
    assert rig.c.state == State.LIVE
    assert rig.c._reattach_failures == 0 and rig.c._reattach_next_try == 0.0


# ---------------------------------------------------------------- SSN self-heal


async def test_ssn_stalled_refreshes_ssn_even_while_live(rig):
    await rig.to_live()
    rig.clear()
    await rig.c.handle(SsnStalled())
    await rig.settle()
    assert rig.names(rig.fb) == ["fb.refresh_ssn"]
    assert rig.c.state == State.LIVE
    assert rig.obs.calls == [] and rig.capture.calls == []


async def test_ssn_stalled_refreshes_at_most_once_a_minute(rig):
    clock = FakeClock()
    rig.c.clock = clock
    await rig.to_ready()
    rig.clear()
    await rig.c.handle(SsnStalled())
    await rig.settle()
    clock.t += 59
    await rig.c.handle(SsnStalled())
    await rig.settle()
    assert rig.names(rig.fb).count("fb.refresh_ssn") == 1
    clock.t += 1
    await rig.c.handle(SsnStalled())
    await rig.settle()
    assert rig.names(rig.fb).count("fb.refresh_ssn") == 2


async def test_ssn_stalled_refresh_failure_is_logged_not_fatal(make, caplog):
    rig = make(fb=FakeFacebook(refresh_ssn_raises=[None, RuntimeError("Target closed")]))
    await rig.to_ready()  # the boot-time refresh succeeds
    await rig.c.handle(SsnStalled())
    await rig.settle()
    assert rig.c.state == State.READY
    assert "Target closed" in caplog.text
