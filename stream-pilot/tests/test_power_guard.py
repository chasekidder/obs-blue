import json

from stream_pilot.__main__ import build_parser
from stream_pilot.power_guard import PressGuard, ending_in_progress


class Rig:
    def __init__(self, *, ending=False, poweroff_ok=True):
        self.ending = ending
        self.poweroffs = 0
        self.poweroff_ok = poweroff_ok
        self.guard = PressGuard(lambda: self.ending, self._poweroff, deadline_s=15.0, grace_s=90.0)

    def _poweroff(self) -> bool:
        self.poweroffs += 1
        return self.poweroff_ok


def test_no_press_no_poweroff():
    r = Rig()
    r.guard.tick(1000.0)
    assert r.poweroffs == 0


def test_press_powers_off_after_the_deadline_if_nothing_else_did():
    r = Rig()
    r.guard.press(10.0)
    r.guard.tick(24.9)
    assert r.poweroffs == 0
    r.guard.tick(25.0)
    assert r.poweroffs == 1
    r.guard.press(26.0)
    r.guard.tick(100.0)
    assert r.poweroffs == 1  # already going down


def test_extra_presses_do_not_push_the_deadline_back():
    r = Rig()
    r.guard.press(10.0)
    r.guard.press(20.0)
    r.guard.tick(25.0)
    assert r.poweroffs == 1


def test_ending_stream_pilot_gets_the_grace_period():
    r = Rig(ending=True)
    r.guard.press(10.0)
    r.guard.tick(25.0)
    assert r.poweroffs == 0
    r.guard.tick(114.9)
    assert r.poweroffs == 0
    r.guard.tick(115.0)
    assert r.poweroffs == 1
    r.guard.tick(500.0)
    assert r.poweroffs == 1


def test_failed_poweroff_can_be_retried_with_another_press():
    r = Rig(poweroff_ok=False)
    r.guard.press(10.0)
    r.guard.tick(25.0)
    assert r.poweroffs == 1
    r.guard.tick(60.0)
    assert r.poweroffs == 1
    r.guard.press(61.0)
    r.guard.tick(76.0)
    assert r.poweroffs == 2


def write_status(tmp_path, **data):
    p = tmp_path / "status.json"
    p.write_text(json.dumps(data))
    return p


def test_ending_in_progress_needs_a_fresh_ending_state(tmp_path):
    assert ending_in_progress(write_status(tmp_path, state="ENDING", updated_at=100.0), now=120.0)
    assert not ending_in_progress(write_status(tmp_path, state="ENDING", updated_at=100.0), now=131.0)
    assert not ending_in_progress(write_status(tmp_path, state="LIVE", updated_at=100.0), now=101.0)
    assert not ending_in_progress(write_status(tmp_path, state="ENDING"), now=101.0)


def test_ending_in_progress_unreadable_status_is_false(tmp_path):
    assert not ending_in_progress(tmp_path / "missing.json", now=1.0)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert not ending_in_progress(bad, now=1.0)
    bad.write_text("[1, 2]")
    assert not ending_in_progress(bad, now=1.0)


def test_cli_has_power_guard_with_user():
    args = build_parser().parse_args(["power-guard", "--user", "stream"])
    assert args.user == "stream"
