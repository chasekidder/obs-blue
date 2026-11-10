import json

from stream_pilot.health import (
    FRESH_WINDOW_S,
    POLL_INTERVAL_S,
    check_health,
    format_status,
)


class FakeClock:
    """Fake now/sleep pair: sleeping advances time and records the call."""

    def __init__(self, start=1_000_000.0):
        self.t = start
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def write_status(path, clock, *, state="LIVE", age=1.0, **extra):
    data = {"state": state, "since": clock.t - 100, "last_error": None,
            "live_id": None, "memory": None, "updated_at": clock.t - age}
    data.update(extra)
    path.write_text(json.dumps(data))


# --- format_status --------------------------------------------------------

def test_format_status_full():
    status = {
        "state": "LIVE",
        "since": 1000.0 - 723,
        "last_error": "camera lost",
        "live_id": "12345",
        "memory": {"mem_available_mb": 2048, "psi": None, "procs": []},
        "updated_at": 1000.0 - 4,
    }
    lines = format_status(status, now=1000.0).splitlines()
    assert lines == [
        "state: LIVE (since 12m 03s)",
        "updated: 4s ago",
        "last error: camera lost",
        "live id: 12345",
        "mem available: 2048 MB",
    ]


def test_format_status_minimal_dash_placeholders():
    status = {"state": "STARTING", "since": 990.0, "last_error": None,
              "live_id": None, "memory": None, "updated_at": 999.0}
    lines = format_status(status, now=1000.0).splitlines()
    assert lines == [
        "state: STARTING (since 10s)",
        "updated: 1s ago",
        "last error: -",
        "live id: -",
        "mem available: -",
    ]


def test_format_status_robust_to_missing_keys():
    text = format_status({}, now=1000.0)
    assert "state: ?" in text
    assert "updated: ?" in text
    assert "last error: -" in text
    assert "live id: -" in text
    assert "mem available: -" in text


def test_format_status_memory_without_available_and_hours():
    status = {"state": "LIVE", "since": 1000.0 - 3725, "memory": {},
              "updated_at": 1000.0}
    lines = format_status(status, now=1000.0).splitlines()
    assert lines[0] == "state: LIVE (since 1h 02m 05s)"
    assert lines[-1] == "mem available: -"


def test_format_status_defaults_now_to_time_time(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "time", lambda: 1000.0)
    text = format_status({"state": "LIVE", "since": 940.0, "updated_at": 995.0})
    assert "since 1m 00s" in text
    assert "updated: 5s ago" in text


# --- check_health ---------------------------------------------------------

def test_fresh_status_is_healthy_immediately(tmp_path):
    clock = FakeClock()
    p = tmp_path / "status.json"
    write_status(p, clock, state="LIVE", age=3)
    assert check_health(p, 300, now=clock.now, sleep=clock.sleep) == 0
    assert clock.sleeps == []


def test_non_starting_states_are_healthy(tmp_path):
    clock = FakeClock()
    p = tmp_path / "status.json"
    for state in ("READY", "LIVE", "ERROR", "SETUP_FAILED"):
        write_status(p, clock, state=state)
        assert check_health(p, 10, now=clock.now, sleep=clock.sleep) == 0


def test_stale_status_unhealthy_after_timeout(tmp_path, capsys):
    clock = FakeClock()
    p = tmp_path / "status.json"
    write_status(p, clock, state="LIVE", age=FRESH_WINDOW_S + 1)
    start = clock.t
    assert check_health(p, 20, now=clock.now, sleep=clock.sleep) == 1
    assert clock.t - start >= 20
    assert set(clock.sleeps) == {POLL_INTERVAL_S}
    assert "stale" in capsys.readouterr().err


def test_freshness_boundary_is_inclusive(tmp_path):
    clock = FakeClock()
    p = tmp_path / "status.json"
    write_status(p, clock, state="LIVE", age=FRESH_WINDOW_S)
    assert check_health(p, 0, now=clock.now, sleep=clock.sleep) == 0


def test_starting_is_unhealthy(tmp_path, capsys):
    clock = FakeClock()
    p = tmp_path / "status.json"
    write_status(p, clock, state="STARTING", age=1)
    assert check_health(p, 12, now=clock.now, sleep=clock.sleep) == 1
    assert "STARTING" in capsys.readouterr().err


def test_missing_file_is_unhealthy(tmp_path, capsys):
    clock = FakeClock()
    assert check_health(tmp_path / "nope.json", 12,
                        now=clock.now, sleep=clock.sleep) == 1
    assert "missing" in capsys.readouterr().err


def test_corrupt_json_counts_as_not_healthy(tmp_path, capsys):
    clock = FakeClock()
    p = tmp_path / "status.json"
    p.write_text("{not json")
    assert check_health(p, 6, now=clock.now, sleep=clock.sleep) == 1
    assert "unreadable" in capsys.readouterr().err


def test_non_object_or_bad_types_not_healthy(tmp_path):
    clock = FakeClock()
    p = tmp_path / "status.json"
    for body in ("[]", '{"state": "LIVE"}', '{"state": "LIVE", "updated_at": "x"}',
                 '{"state": 5, "updated_at": 1}'):
        p.write_text(body)
        assert check_health(p, 0, now=clock.now, sleep=clock.sleep) == 1


def test_checks_once_even_with_zero_timeout(tmp_path):
    clock = FakeClock()
    p = tmp_path / "status.json"
    write_status(p, clock)
    assert check_health(p, 0, now=clock.now, sleep=clock.sleep) == 0
    assert clock.sleeps == []


def test_becomes_healthy_after_a_few_polls(tmp_path):
    clock = FakeClock()
    p = tmp_path / "status.json"
    write_status(p, clock, state="STARTING")
    calls = {"n": 0}
    real_sleep = clock.sleep

    def sleep(s):
        real_sleep(s)
        calls["n"] += 1
        if calls["n"] == 3:
            write_status(p, clock, state="READY", age=0)

    assert check_health(p, 300, now=clock.now, sleep=sleep) == 0
    assert calls["n"] == 3
    assert clock.sleeps == [POLL_INTERVAL_S] * 3


def test_does_not_overshoot_timeout_with_sleeps(tmp_path):
    clock = FakeClock()
    check_health(tmp_path / "nope.json", 7, now=clock.now, sleep=clock.sleep)
    assert sum(clock.sleeps) <= 7 + 1e-9
