import asyncio
import logging
import subprocess
from types import SimpleNamespace

import pytest

from stream_pilot.events import PowerPress
from stream_pilot.power import (
    EV_KEY,
    KEY_POWER,
    PowerKeyListener,
    find_power_devices,
)
from stream_pilot.system import SystemCtl, V4l2Camera


# ---------------------------------------------------------------- fakes


class FakeDevice:
    def __init__(self, name="Power Button", caps=None, events=None, error=None):
        self.name = name
        self._caps = {EV_KEY: [KEY_POWER]} if caps is None else caps
        self._events = events or []
        self._error = error
        self.closed = False

    def capabilities(self):
        return self._caps

    def close(self):
        self.closed = True

    async def async_read_loop(self):
        for ev in self._events:
            yield ev
        if self._error is not None:
            raise self._error


def ev(value, code=KEY_POWER, type_=EV_KEY):
    return SimpleNamespace(type=type_, code=code, value=value)


def make_open(devices: dict):
    return lambda path: devices[path]


# ---------------------------------------------------------------- find_power_devices


def test_find_power_devices_filters_by_name_and_capability():
    power = FakeDevice()
    kbd = FakeDevice(name="AT Keyboard", caps={EV_KEY: [30, 31]})
    fake_named = FakeDevice(name="Power Button", caps={EV_KEY: [30]})  # no KEY_POWER
    no_key = FakeDevice(name="Power Button", caps={})
    devices = {"/d/0": kbd, "/d/1": power, "/d/2": fake_named, "/d/3": no_key}
    found = find_power_devices(
        list_devices=lambda: list(devices), open_device=make_open(devices)
    )
    assert found == [power]
    assert not power.closed
    assert kbd.closed and fake_named.closed and no_key.closed


def test_find_power_devices_skips_unopenable_devices():
    power = FakeDevice()

    def opener(path):
        if path == "/d/0":
            raise PermissionError("nope")
        return power

    found = find_power_devices(list_devices=lambda: ["/d/0", "/d/1"], open_device=opener)
    assert found == [power]


def test_find_power_devices_none():
    assert find_power_devices(list_devices=lambda: [], open_device=make_open({})) == []


# ---------------------------------------------------------------- PowerKeyListener


class Stop(Exception):
    pass


def collecting_listener(find_devices, sleeps=None, stop_after=1):
    events = []
    sleeps = sleeps if sleeps is not None else []

    async def sleep(s):
        sleeps.append(s)
        if len(sleeps) >= stop_after:
            raise Stop

    lst = PowerKeyListener(find_devices=find_devices, sleep=sleep)
    lst.set_event_sink(events.append)
    return lst, events, sleeps


async def test_emits_exactly_one_powerpress_for_value_1():
    dev = FakeDevice(events=[ev(1), ev(0), ev(2)])
    lst, events, _ = collecting_listener(lambda: [dev] if not dev.closed else [])
    with pytest.raises(Stop):
        await lst.start()
    assert events == [PowerPress()]


async def test_ignores_other_keys_and_event_types():
    dev = FakeDevice(
        events=[ev(1, code=30), ev(1, type_=0), ev(0), ev(2), ev(1)],
    )
    lst, events, _ = collecting_listener(lambda: [dev] if not dev.closed else [])
    with pytest.raises(Stop):
        await lst.start()
    assert events == [PowerPress()]


async def test_reads_multiple_devices_concurrently():
    a = FakeDevice(events=[ev(1)])
    b = FakeDevice(events=[ev(1)])
    lst, events, _ = collecting_listener(
        lambda: [a, b] if not (a.closed and b.closed) else []
    )
    with pytest.raises(Stop):
        await lst.start()
    assert events == [PowerPress(), PowerPress()]


async def test_no_devices_warns_and_retries_every_10s(caplog):
    calls = []

    def find():
        calls.append(1)
        return []

    lst, events, sleeps = collecting_listener(find, stop_after=3)
    with caplog.at_level(logging.WARNING), pytest.raises(Stop):
        await lst.start()
    assert sleeps == [10, 10, 10]
    assert len(calls) == 3
    assert events == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_device_gone_oserror_returns_to_discovery(caplog):
    gone = FakeDevice(events=[ev(1)], error=OSError(19, "No such device"))
    back = FakeDevice(events=[ev(1)])
    seq = [[gone], [back]]
    calls = []

    def find():
        calls.append(1)
        return seq.pop(0) if seq else []

    lst, events, sleeps = collecting_listener(find, stop_after=3)
    with caplog.at_level(logging.WARNING), pytest.raises(Stop):
        await lst.start()
    # press before loss, rediscovery, press from the re-found device, then none -> 10s
    assert events == [PowerPress(), PowerPress()]
    assert gone.closed and back.closed
    assert len(calls) == 3
    assert sleeps == [1, 1, 10]
    assert any("No such device" in r.getMessage() for r in caplog.records)


async def test_oserror_on_one_device_cancels_others_and_closes_all():
    class Hang(FakeDevice):
        async def async_read_loop(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    gone = FakeDevice(error=OSError(19, "gone"))
    hang = Hang()
    seq = [[gone, hang]]

    lst, _, _ = collecting_listener(lambda: seq.pop(0) if seq else [], stop_after=1)
    with pytest.raises(Stop):
        await lst.start()
    assert gone.closed and hang.closed


async def test_default_sink_is_noop():
    dev = FakeDevice(events=[ev(1)])
    lst = PowerKeyListener(find_devices=lambda: [dev] if not dev.closed else [])

    async def boom(_):
        raise Stop

    lst._sleep = boom
    with pytest.raises(Stop):
        await lst.start()


# ---------------------------------------------------------------- system adapters


class FakeProc:
    def __init__(self, returncode=0, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout

    async def wait(self):
        return self.returncode

    async def communicate(self):
        return self.stdout, b""


class FakeExec:
    def __init__(self, returncode=0, raises=None, stdout=b""):
        self.calls = []
        self.kwargs = []
        self.returncode = returncode
        self.raises = raises
        self.stdout = stdout

    async def __call__(self, *argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        if self.raises:
            raise self.raises
        return FakeProc(self.returncode, self.stdout)


async def test_systemctl_poweroff_argv():
    ex = FakeExec()
    await SystemCtl(exec_fn=ex).poweroff()
    assert ex.calls == [["systemctl", "poweroff"]]
    assert ex.kwargs[0]["stdout"] == subprocess.DEVNULL
    assert ex.kwargs[0]["stderr"] == subprocess.DEVNULL


async def test_systemctl_stop_user_unit_argv():
    ex = FakeExec()
    await SystemCtl(exec_fn=ex).stop_user_unit("obs.service")
    assert ex.calls == [["systemctl", "--user", "stop", "obs.service"]]


async def test_v4l2_autofocus_on_argv():
    ex = FakeExec()
    await V4l2Camera(exec_fn=ex).set_autofocus("/dev/video0", True)
    assert ex.calls == [
        ["v4l2-ctl", "-d", "/dev/video0", "--set-ctrl=focus_automatic_continuous=1"]
    ]


async def test_v4l2_autofocus_off_argv():
    ex = FakeExec()
    await V4l2Camera(exec_fn=ex).set_autofocus("/dev/video2", False)
    assert ex.calls == [
        ["v4l2-ctl", "-d", "/dev/video2", "--set-ctrl=focus_automatic_continuous=0"]
    ]


async def test_v4l2_macro_turns_autofocus_off_then_focuses_closest():
    from pathlib import Path

    ex = FakeExec(stdout=(Path(__file__).parent / "data" / "v4l2-c920-ctrls.txt").read_bytes())
    await V4l2Camera(exec_fn=ex).set_macro("/dev/video0")
    assert ex.calls == [
        ["v4l2-ctl", "-d", "/dev/video0", "--set-ctrl=focus_automatic_continuous=0"],
        ["v4l2-ctl", "-d", "/dev/video0", "-l"],
        ["v4l2-ctl", "-d", "/dev/video0", "--set-ctrl=focus_absolute=250"],
    ]


async def test_v4l2_macro_without_a_focus_range_is_logged(caplog):
    ex = FakeExec(stdout=b"brightness 0x00980900 (int) : min=0 max=255\n")
    await V4l2Camera(exec_fn=ex).set_macro("/dev/video0")
    assert len(ex.calls) == 2
    assert "no focus_absolute range" in caplog.text


@pytest.mark.parametrize("out, want", [
    (b"focus_automatic_continuous: 1\n", True),
    (b"focus_automatic_continuous: 0\n", False),
    (b"garbage\n", None),
])
async def test_v4l2_get_autofocus_parses_value(out, want):
    ex = FakeExec(stdout=out)
    assert await V4l2Camera(exec_fn=ex).get_autofocus("/dev/video0") is want
    assert ex.calls == [
        ["v4l2-ctl", "-d", "/dev/video0", "--get-ctrl=focus_automatic_continuous"]
    ]
    assert ex.kwargs[0]["stdout"] == subprocess.PIPE


async def test_v4l2_get_autofocus_failure_is_none(caplog):
    with caplog.at_level(logging.WARNING):
        assert await V4l2Camera(exec_fn=FakeExec(returncode=1)).get_autofocus("/dev/video0") is None
        missing = FakeExec(raises=FileNotFoundError(2, "No such file", "v4l2-ctl"))
        assert await V4l2Camera(exec_fn=missing).get_autofocus("/dev/video0") is None
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 2


async def test_nonzero_exit_logged_not_raised(caplog):
    ex = FakeExec(returncode=1)
    with caplog.at_level(logging.WARNING):
        await SystemCtl(exec_fn=ex).stop_user_unit("x.service")
        await V4l2Camera(exec_fn=ex).set_autofocus("/dev/video0", True)
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("systemctl" in m and "x.service" in m and "1" in m for m in msgs)
    assert any("v4l2-ctl" in m and "/dev/video0" in m for m in msgs)


async def test_missing_binary_logged_not_raised(caplog):
    ex = FakeExec(raises=FileNotFoundError(2, "No such file", "v4l2-ctl"))
    with caplog.at_level(logging.WARNING):
        await V4l2Camera(exec_fn=ex).set_autofocus("/dev/video0", False)
        await SystemCtl(exec_fn=ex).poweroff()
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 2
