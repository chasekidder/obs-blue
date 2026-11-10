import json
import logging
import time

from stream_pilot.events import DeckPress, ObsStreamState, PowerPress, RetryRequested, Tick
from stream_pilot.persist import Saved, StateStore, StatusWriter
from stream_pilot.ports import StepFailed, StreamTarget
from stream_pilot.states import LIVE_STATES, STATUS_FACES, KeyFace, State


def test_round_trip(tmp_path):
    store = StateStore(tmp_path / "state.json")
    saved = Saved(state="LIVE", live_id="123", stream_dir="/tmp/x")
    store.save(saved)
    assert store.load() == saved


def test_round_trip_defaults(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save(Saved(state="READY"))
    assert store.load() == Saved(state="READY", live_id=None, stream_dir=None)


def test_missing_returns_none(tmp_path):
    assert StateStore(tmp_path / "nope.json").load() is None


def test_corrupt_json_returns_none_and_warns(tmp_path, caplog):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    with caplog.at_level(logging.WARNING, logger="stream_pilot.persist"):
        assert StateStore(p).load() is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_wrong_shape_returns_none(tmp_path):
    p = tmp_path / "state.json"
    for body in ('[1, 2]', '{"live_id": "1"}', '{"state": 5}', '"x"'):
        p.write_text(body)
        assert StateStore(p).load() is None


def test_save_creates_parent_and_leaves_no_tmp(tmp_path):
    path = tmp_path / "a" / "b" / "state.json"
    store = StateStore(path)
    store.save(Saved(state="LIVE", live_id="1"))
    store.save(Saved(state="ENDING", live_id="1"))
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]
    assert not list(path.parent.glob("*.tmp"))
    assert store.load().state == "ENDING"


def test_status_writer_keys(tmp_path):
    path = tmp_path / "run" / "status.json"
    before = time.time()
    StatusWriter(path).write(
        state="LIVE", since=100.0, last_error="boom", live_id="9", memory={"psi": 1.5}
    )
    data = json.loads(path.read_text())
    assert set(data) == {"state", "since", "last_error", "live_id", "memory", "updated_at"}
    assert data["state"] == "LIVE"
    assert data["since"] == 100.0
    assert data["last_error"] == "boom"
    assert data["live_id"] == "9"
    assert data["memory"] == {"psi": 1.5}
    assert data["updated_at"] >= before
    assert not list(path.parent.glob("*.tmp"))


def test_status_writer_nulls_and_overwrite(tmp_path):
    path = tmp_path / "status.json"
    w = StatusWriter(path)
    w.write(state="READY", since=1.0, last_error=None, live_id=None, memory=None)
    w.write(state="LIVE", since=2.0, last_error=None, live_id="1", memory=None)
    data = json.loads(path.read_text())
    assert data["state"] == "LIVE"
    assert data["last_error"] is None and data["memory"] is None
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_state_faces_complete():
    assert set(STATUS_FACES) == set(State)
    assert LIVE_STATES == {State.LIVE, State.RECONNECTING}
    assert STATUS_FACES[State.READY] == KeyFace("GO LIVE", "#1e9e3e")
    assert STATUS_FACES[State.GOING_LIVE].blink is True
    assert STATUS_FACES[State.LIVE].fg == "#ffffff"
    assert State.LIVE == "LIVE"


def test_events_and_ports_basics():
    assert DeckPress(3) == DeckPress(key=3, long=False)
    assert ObsStreamState(True).reconnecting is False
    assert PowerPress() == PowerPress()
    assert RetryRequested() == RetryRequested() and Tick() == Tick()
    err = StepFailed("preview", "timeout")
    assert err.step == "preview" and err.detail == "timeout"
    assert str(err) == "preview: timeout"
    assert StreamTarget("rtmps://x", "k").key == "k"
