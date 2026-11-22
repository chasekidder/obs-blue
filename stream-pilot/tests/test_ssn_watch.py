import asyncio

import pytest

from stream_pilot.events import SsnStalled
from stream_pilot.ssn_watch import DBUS_MONITOR_ARGV, SsnStallWatcher

# dbus-monitor's rendering of Notify calls (app name, icon, summary, body, ...).
SSN_WARNING = [
    "method call time=1795000000.1 sender=:1.4183 -> destination=:1.30 serial=50 "
    "path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=Notify",
    '   string "Chromium"',
    "   uint32 0",
    '   string "file:///tmp/org.chromium.Chromium.scoped_dir.x/logo.png"',
    '   string "Social Stream background stopped"',
    '   string "Could not reopen the background page. Open the extension popup to restart capture."',
]
OTHER_NOTIFICATION = [
    "method call time=1795000001.1 sender=:1.4183 -> destination=:1.30 serial=51 "
    "path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=Notify",
    '   string "Chromium"',
    '   string "Something else"',
    '   string "this body mentions Social Stream background stopped"',
]


class FakeProc:
    def __init__(self, lines, *, exits=True):
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(line.encode() + b"\n")
        if exits:
            self.stdout.feed_eof()
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.stdout.feed_eof()

    async def wait(self):
        return self.returncode if self.returncode is not None else 0


class Stop(Exception):
    pass


def make_watcher(procs):
    argvs, events = [], []

    async def spawn(*argv):
        argvs.append(argv)
        if not procs:
            raise Stop
        return procs.pop(0)

    async def sleep(_s):
        if not procs:
            raise Stop

    w = SsnStallWatcher(spawn=spawn, sleep=sleep)
    w.set_event_sink(events.append)
    return w, argvs, events


async def test_only_ssns_background_stopped_warning_is_an_event():
    w, argvs, events = make_watcher([FakeProc(OTHER_NOTIFICATION + SSN_WARNING + SSN_WARNING)])
    with pytest.raises(Stop):
        await w.start()
    assert events == [SsnStalled(), SsnStalled()]
    assert argvs[0] == DBUS_MONITOR_ARGV
    assert argvs[0][:2] == ("dbus-monitor", "--session")


async def test_restarts_dbus_monitor_when_it_exits():
    w, argvs, events = make_watcher([FakeProc([]), FakeProc(SSN_WARNING)])
    with pytest.raises(Stop):
        await w.start()
    assert len(argvs) == 2  # restarted once after the first exited
    assert events == [SsnStalled()]


async def test_missing_dbus_monitor_is_retried_not_fatal():
    calls = []

    async def spawn(*argv):
        calls.append(argv)
        if len(calls) == 1:
            raise FileNotFoundError("dbus-monitor")
        raise Stop

    async def sleep(_s):
        pass

    w = SsnStallWatcher(spawn=spawn, sleep=sleep)
    with pytest.raises(Stop):
        await w.start()
    assert len(calls) == 2


async def test_cancel_terminates_dbus_monitor():
    proc = FakeProc(SSN_WARNING, exits=False)
    w, argvs, events = make_watcher([proc])
    task = asyncio.ensure_future(w.start())
    for _ in range(50):
        if events:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == [SsnStalled()]
    assert proc.terminated
