import asyncio
import logging
from dataclasses import dataclass

import pytest

from stream_pilot.config import ObsCfg
from stream_pilot.events import ObsStreamState
from stream_pilot.obs import SimpleObsClient, map_stream_state
from stream_pilot.ports import StepFailed, StreamTarget

CFG = ObsCfg(host="127.0.0.1", port=4455, password="pw")


@dataclass
class Req:
    requestType: str
    requestData: dict | None = None


@dataclass
class Status:
    result: bool = True
    code: int = 100
    comment: str | None = None


@dataclass
class Resp:
    requestStatus: Status
    responseData: dict | None = None

    def ok(self):
        return self.requestStatus.result


def ok(data=None):
    return Resp(Status(True, 100), data)


def fail(code, comment):
    return Resp(Status(False, code, comment))


class FakeWs:
    """Stands in for simpleobsws.WebSocketClient."""

    def __init__(self):
        self.identified = False
        self.connects = 0
        self.disconnects = 0
        self.connect_errors = []  # popped per connect(); exception or None
        self.identify_ok = True
        self.calls = []
        self.responses = {}  # requestType -> Resp | Exception | list of them
        self.callbacks = []

    async def connect(self):
        self.connects += 1
        if self.connect_errors:
            err = self.connect_errors.pop(0)
            if err:
                raise err
        return True

    async def wait_until_identified(self, timeout=10):
        self.identified = self.identify_ok
        return self.identify_ok

    async def disconnect(self):
        self.disconnects += 1
        self.identified = False
        return True

    async def call(self, request, timeout=15):
        self.calls.append((request.requestType, request.requestData))
        r = self.responses.get(request.requestType, ok())
        if isinstance(r, list):
            r = r.pop(0)
        if isinstance(r, Exception):
            self.identified = False
            raise r
        return r

    def register_event_callback(self, callback, event=None):
        self.callbacks.append((callback, event))


def make(ws=None):
    ws = ws or FakeWs()
    return SimpleObsClient(CFG, client=ws, request_factory=Req), ws


# ---- map_stream_state -------------------------------------------------


@pytest.mark.parametrize(
    "state,expected",
    [
        ("OBS_WEBSOCKET_OUTPUT_STARTED", ObsStreamState(True, False)),
        ("OBS_WEBSOCKET_OUTPUT_RECONNECTING", ObsStreamState(True, True)),
        ("OBS_WEBSOCKET_OUTPUT_RECONNECTED", ObsStreamState(True, False)),
        ("OBS_WEBSOCKET_OUTPUT_STOPPED", ObsStreamState(False, False)),
        ("OBS_WEBSOCKET_OUTPUT_STARTING", None),
        ("OBS_WEBSOCKET_OUTPUT_STOPPING", None),
        ("OBS_WEBSOCKET_OUTPUT_PAUSED", None),
        ("", None),
    ],
)
def test_map_stream_state_table(state, expected):
    assert map_stream_state({"outputState": state, "outputActive": True}) == expected


def test_map_stream_state_missing_or_bad_data():
    assert map_stream_state({}) is None
    assert map_stream_state(None) is None


# ---- requests ---------------------------------------------------------


async def test_set_stream_target_exact_payload():
    obs, ws = make()
    await obs.connect()
    await obs.set_stream_target(StreamTarget("rtmps://live.example/x", "KEY"))
    assert ws.calls == [
        (
            "SetStreamServiceSettings",
            {
                "streamServiceType": "rtmp_custom",
                "streamServiceSettings": {
                    "server": "rtmps://live.example/x",
                    "key": "KEY",
                },
            },
        )
    ]


@pytest.mark.parametrize("active", [True, False])
async def test_stream_active_reads_output_active(active):
    obs, ws = make()
    ws.responses["GetStreamStatus"] = ok({"outputActive": active, "outputReconnecting": False})
    await obs.connect()
    assert await obs.stream_active() is active
    assert ws.calls == [("GetStreamStatus", None)]


async def test_stream_active_missing_field_raises():
    obs, ws = make()
    ws.responses["GetStreamStatus"] = ok(None)
    with pytest.raises(StepFailed) as ei:
        await obs.stream_active()
    assert ei.value.step == "obs"


async def test_scene_requests():
    obs, ws = make()
    ws.responses["GetCurrentProgramScene"] = ok({"currentProgramSceneName": "Black"})
    await obs.set_scene("Main Camera")
    assert await obs.current_scene() == "Black"
    assert ws.calls == [
        ("SetCurrentProgramScene", {"sceneName": "Main Camera"}),
        ("GetCurrentProgramScene", None),
    ]


async def test_current_scene_falls_back_to_scene_name():
    obs, ws = make()
    ws.responses["GetCurrentProgramScene"] = ok({"sceneName": "Black"})
    assert await obs.current_scene() == "Black"


async def test_start_and_stop_requests():
    obs, ws = make()
    await obs.start_stream()
    await obs.stop_stream()
    assert [c[0] for c in ws.calls] == ["StartStream", "StopStream"]


async def test_request_failure_raises_step_failed():
    obs, ws = make()
    ws.responses["SetCurrentProgramScene"] = fail(600, "No scene was found")
    with pytest.raises(StepFailed) as ei:
        await obs.set_scene("Nope")
    assert ei.value.step == "obs"
    assert "SetCurrentProgramScene: 600 No scene was found" in ei.value.detail


async def test_start_when_running_is_idempotent(caplog):
    obs, ws = make()
    ws.responses["StartStream"] = fail(500, "Output is running")
    with caplog.at_level(logging.INFO, logger="stream_pilot.obs"):
        await obs.start_stream()
    assert any(r.levelno == logging.INFO for r in caplog.records)


async def test_stop_when_not_running_is_idempotent():
    obs, ws = make()
    ws.responses["StopStream"] = fail(501, "Output is not running")
    await obs.stop_stream()


async def test_idempotent_codes_are_not_swapped():
    obs, ws = make()
    ws.responses["StartStream"] = fail(501, "x")
    ws.responses["StopStream"] = fail(500, "x")
    with pytest.raises(StepFailed):
        await obs.start_stream()
    with pytest.raises(StepFailed):
        await obs.stop_stream()


# ---- connection handling ---------------------------------------------


async def test_connect_registers_event_callback_once_and_identifies():
    obs, ws = make()
    await obs.connect()
    await obs.connect()  # already identified: no reconnect
    assert ws.connects == 1
    assert [e for _, e in ws.callbacks] == ["StreamStateChanged"]


async def test_first_request_connects_lazily():
    obs, ws = make()
    await obs.set_scene("Black")
    assert ws.connects == 1


async def test_reconnects_once_on_disconnect_then_succeeds():
    obs, ws = make()
    await obs.connect()
    ws.responses["GetStreamStatus"] = [
        ConnectionError("closed"),
        ok({"outputActive": True}),
    ]
    assert await obs.stream_active() is True
    assert ws.connects == 2
    assert [c[0] for c in ws.calls] == ["GetStreamStatus", "GetStreamStatus"]


async def test_reconnect_when_already_disconnected_before_call():
    obs, ws = make()
    await obs.connect()
    ws.identified = False  # recv task noticed the close
    ws.responses["GetStreamStatus"] = ok({"outputActive": False})
    assert await obs.stream_active() is False
    assert ws.connects == 2
    assert len(ws.calls) == 1


async def test_reconnect_failure_raises_step_failed():
    obs, ws = make()
    await obs.connect()
    ws.responses["StartStream"] = [ConnectionError("closed")]
    ws.connect_errors = [ConnectionRefusedError("down")]
    with pytest.raises(StepFailed) as ei:
        await obs.start_stream()
    assert ei.value.step == "obs"


async def test_retry_after_reconnect_failing_again_raises():
    obs, ws = make()
    await obs.connect()
    ws.responses["StartStream"] = [ConnectionError("a"), ConnectionError("b")]
    with pytest.raises(StepFailed) as ei:
        await obs.start_stream()
    assert ei.value.step == "obs"
    assert ws.connects == 2  # exactly one reconnect, no loop


async def test_connect_failure_raises_step_failed():
    obs, ws = make()
    ws.connect_errors = [OSError("refused")]
    with pytest.raises(StepFailed) as ei:
        await obs.connect()
    assert ei.value.step == "obs"


async def test_identify_failure_raises_and_cleans_up():
    obs, ws = make()
    ws.identify_ok = False
    with pytest.raises(StepFailed) as ei:
        await obs.connect()
    assert ei.value.step == "obs"
    assert ws.disconnects >= 1


async def test_connect_hang_is_bounded():
    class Hang(FakeWs):
        async def connect(self):
            await asyncio.sleep(60)

    ws = Hang()
    obs = SimpleObsClient(CFG, client=ws, request_factory=Req, connect_timeout_s=0.05)
    with pytest.raises(StepFailed) as ei:
        await obs.connect()
    assert ei.value.step == "obs"
    assert "timed out" in ei.value.detail


# ---- events -----------------------------------------------------------


async def test_event_callback_maps_and_calls_sink():
    obs, ws = make()
    got = []
    obs.set_event_sink(got.append)
    await obs.connect()
    (cb, name), = ws.callbacks
    assert name == "StreamStateChanged"
    await cb({"outputState": "OBS_WEBSOCKET_OUTPUT_RECONNECTING", "outputActive": True})
    await cb({"outputState": "OBS_WEBSOCKET_OUTPUT_STARTING", "outputActive": False})
    await cb({"outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED", "outputActive": False})
    assert got == [ObsStreamState(True, True), ObsStreamState(False, False)]


async def test_event_callback_without_sink_is_noop():
    obs, ws = make()
    await obs.connect()
    (cb, _), = ws.callbacks
    await cb({"outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"})


async def test_sink_exception_is_logged_not_raised(caplog):
    obs, ws = make()

    def boom(ev):
        raise RuntimeError("sink broke")

    obs.set_event_sink(boom)
    await obs.connect()
    (cb, _), = ws.callbacks
    with caplog.at_level(logging.ERROR, logger="stream_pilot.obs"):
        await cb({"outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"})
    assert any("sink" in r.getMessage() for r in caplog.records)
