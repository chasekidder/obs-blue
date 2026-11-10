"""SSN chat listener: parsing + a listener against a LOCAL websocket server.

Host safety: only 127.0.0.1 is ever contacted; never socialstream.ninja.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from stream_pilot.comment_log import CommentLog
from stream_pilot.ssn_listener import SsnListener, parse_ssn_message

SESSION = "fakeSessionIdAbc123"


# -- parse_ssn_message ------------------------------------------------------


def test_parse_plain():
    c = parse_ssn_message(
        json.dumps({"chatname": "Ann", "chatmessage": "sold!"})
    )
    assert (c.author, c.text) == ("Ann", "sold!")
    assert c.fb_comment_id is None and c.created_at is None


def test_parse_id_and_timestamp_stringified():
    c = parse_ssn_message(
        json.dumps(
            {"chatname": "Ann", "chatmessage": "hi", "id": 42,
             "timestamp": 1700000000123, "type": "facebook"}
        )
    )
    assert c.fb_comment_id == "42"
    assert c.created_at == "1700000000123"


@pytest.mark.parametrize("key", ["msg", "overlayNinja", "data"])
def test_parse_wrapped(key):
    c = parse_ssn_message(
        json.dumps({key: {"chatname": "Bo", "chatmessage": "mine", "id": "x1"}})
    )
    assert (c.author, c.text, c.fb_comment_id) == ("Bo", "mine", "x1")


def test_parse_strips_tags_unescapes_and_collapses_whitespace():
    c = parse_ssn_message(
        json.dumps(
            {
                "chatname": " Cy &amp; Di ",
                "chatmessage": '<b>I   want</b> <img src="x.png"/> it &lt;3\n&quot;now&quot;',
            }
        )
    )
    assert c.author == "Cy & Di"
    assert c.text == 'I want it <3 "now"'


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"chatname": "Ann"},
        {"chatmessage": "hi"},
        {"chatname": "", "chatmessage": "hi"},
        {"chatname": "Ann", "chatmessage": ""},
        {"chatname": "Ann", "chatmessage": "   "},
        {"chatname": "Ann", "chatmessage": "<img src='x'/>"},
        {"chatname": None, "chatmessage": "hi"},
        {"chatname": "Ann", "chatmessage": 5},
        {"msg": {"chatname": "Ann"}},
        {"msg": "not a dict"},
    ],
)
def test_parse_missing_or_empty_fields(payload):
    assert parse_ssn_message(json.dumps(payload)) is None


@pytest.mark.parametrize(
    "raw",
    ["", "not json", "{", "null", "42", '"str"', "[]",
     '[{"chatname": "A", "chatmessage": "b"}]'],
)
def test_parse_invalid_or_non_dict(raw):
    assert parse_ssn_message(raw) is None


def test_parse_never_raises_on_odd_input():
    assert parse_ssn_message(None) is None  # type: ignore[arg-type]
    assert parse_ssn_message(b"\xff\xfe") is None  # type: ignore[arg-type]


# -- listener helpers -----------------------------------------------------------


def msg(author, text, **extra):
    return json.dumps({"chatname": author, "chatmessage": text, **extra})


def make_sleep():
    delays: list[float] = []

    async def sleep(d):
        delays.append(d)
        await asyncio.sleep(0)

    sleep.delays = delays
    return sleep


async def wait_for(pred, timeout=5.0):
    async def poll():
        while not pred():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


async def stop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def read_lines(path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


@pytest.fixture
async def server():
    """Local server: conn #1 sends two messages then closes; later
    connections send one message each and stay open until the client leaves."""
    import websockets

    connections: list[str] = []
    paths: list[str] = []

    async def handler(ws):
        n = len(connections)
        connections.append("c")
        paths.append(ws.request.path)
        if n == 0:
            await ws.send(msg("Ann", "first", id=1))
            await ws.send(msg("Bo", "second <b>bold</b>", id=2))
            await ws.close()
        else:
            await ws.send(msg("Cy", "third", id=3))
            await ws.send(b'{"chatname": "Di", "chatmessage": "binary \xc3\xa9"}')
            await ws.send("garbage that is not json")
            try:
                await ws.wait_closed()
            except Exception:
                pass

    async with websockets.serve(handler, "127.0.0.1", 0) as srv:
        yield SimpleNamespace(
            port=srv.sockets[0].getsockname()[1],
            connections=connections,
            paths=paths,
        )


def listener_for(server, log_factory, sleep, **kw):
    return SsnListener(
        SESSION,
        log_factory,
        url_template="ws://127.0.0.1:{port}/{session}",
        port=server.port,
        sleep=sleep,
        **kw,
    )


# -- listener against a local server ------------------------------------------


async def test_delivers_and_reconnects_after_server_close(
    server, tmp_path, caplog
):
    caplog.set_level(logging.INFO)
    path = tmp_path / "comments.jsonl"
    cl = CommentLog(path, "live1")
    sleep = make_sleep()
    task = asyncio.create_task(listener_for(server, lambda: cl, sleep).run())
    await wait_for(lambda: len(read_lines(path)) >= 4)
    await stop(task)

    rows = read_lines(path)
    assert [(r["author"], r["text"]) for r in rows] == [
        ("Ann", "first"),
        ("Bo", "second bold"),
        ("Cy", "third"),
        ("Di", "binary é"),
    ]
    assert all(r["source"] == "ssn" and r["live_id"] == "live1" for r in rows)
    assert rows[0]["fb_comment_id"] == "1"
    assert len(server.connections) == 2
    assert server.paths == [f"/{SESSION}"] * 2
    # reset to 1 s because messages arrived before the close
    assert sleep.delays[0] == 1.0
    # session id is a credential: never in logs
    assert SESSION not in caplog.text
    assert "***" in caplog.text


async def test_log_factory_none_drops_messages(server, tmp_path):
    path = tmp_path / "comments.jsonl"
    live = {"log": None}
    calls = {"n": 0}
    release = asyncio.Event()
    sleeping = asyncio.Event()

    def factory():
        calls["n"] += 1
        return live["log"]

    async def gated_sleep(_d):
        # hold the reconnect until the stream "goes live"
        sleeping.set()
        await release.wait()

    task = asyncio.create_task(listener_for(server, factory, gated_sleep).run())
    await asyncio.wait_for(sleeping.wait(), 5)  # conn #1 fully consumed
    assert calls["n"] == 2  # both messages reached the factory, then dropped
    assert not path.exists()
    live["log"] = CommentLog(path, "live2")
    release.set()
    await wait_for(lambda: len(read_lines(path)) >= 1)
    await stop(task)
    texts = [r["text"] for r in read_lines(path)]
    assert "first" not in texts and "second bold" not in texts
    assert "third" in texts


async def test_append_error_does_not_kill_listener(server, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    path = tmp_path / "comments.jsonl"
    cl = CommentLog(path, "live3")
    real = cl.append_ssn
    state = {"failed": False}

    def flaky(c):
        if not state["failed"]:
            state["failed"] = True
            raise OSError(f"disk full for {SESSION}")
        real(c)

    cl.append_ssn = flaky  # type: ignore[method-assign]
    task = asyncio.create_task(
        listener_for(server, lambda: cl, make_sleep()).run()
    )
    await wait_for(lambda: len(read_lines(path)) >= 3)
    await stop(task)
    assert [r["text"] for r in read_lines(path)][:2] == ["second bold", "third"]
    assert "dropped message" in caplog.text
    assert SESSION not in caplog.text


# -- backoff / error handling with a fake connect -----------------------------------


class FakeWs:
    def __init__(self, frames):
        self._frames = list(frames)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


class FakeConnect:
    """Each plan item: an Exception to raise on connect, or a list of frames."""

    def __init__(self, plan, on_exhausted=None):
        self.plan = list(plan)
        self.urls: list[str] = []
        self.kwargs: list[dict] = []
        self.closed = 0
        self.on_exhausted = on_exhausted

    def __call__(self, url, **kw):
        self.urls.append(url)
        self.kwargs.append(kw)
        return self

    async def __aenter__(self):
        if not self.plan:
            if self.on_exhausted:
                self.on_exhausted()
            await asyncio.sleep(3600)
        item = self.plan.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeWs(item)

    async def __aexit__(self, *exc):
        self.closed += 1
        return False


async def test_backoff_doubles_to_cap_and_resets_after_message(tmp_path):
    cl = CommentLog(tmp_path / "c.jsonl", "live")
    done = asyncio.Event()
    plan = [OSError("x")] * 7 + [[msg("A", "hi")], OSError("y")]
    fc = FakeConnect(plan, on_exhausted=done.set)
    sleep = make_sleep()
    lst = SsnListener(
        SESSION, lambda: cl, connect=fc, sleep=sleep
    )
    task = asyncio.create_task(lst.run())
    await asyncio.wait_for(done.wait(), 5)
    await stop(task)
    # 7 failures: 1,2,4,8,16,30,30; message resets -> 1; failure after -> 2
    assert sleep.delays[:9] == [1, 2, 4, 8, 16, 30, 30, 1, 2]
    assert fc.kwargs[0]["open_timeout"] == 10
    assert fc.urls[0] == f"wss://io.socialstream.ninja/join/{SESSION}/4/1"
    assert fc.closed == 1  # the one successful connection was released


async def test_reset_after_long_connection_without_messages(tmp_path):
    t = {"now": 0.0}
    done = asyncio.Event()

    class SlowWs(FakeWs):
        async def __anext__(self):
            t["now"] += 31  # connection "lasted" 31 s, no messages
            raise StopAsyncIteration

    class C(FakeConnect):
        async def __aenter__(self):
            if len(self.urls) > 3:
                done.set()
                await asyncio.sleep(3600)
            return SlowWs([])

    fc = C([])
    sleep = make_sleep()
    lst = SsnListener(
        SESSION, lambda: None, connect=fc, sleep=sleep, clock=lambda: t["now"]
    )
    task = asyncio.create_task(lst.run())
    await asyncio.wait_for(done.wait(), 5)
    await stop(task)
    assert sleep.delays[:3] == [1, 1, 1]


async def test_connect_error_logs_redacted(caplog):
    caplog.set_level(logging.INFO)
    done = asyncio.Event()
    fc = FakeConnect(
        [OSError(f"cannot reach wss://x/join/{SESSION}/4/1")],
        on_exhausted=done.set,
    )
    lst = SsnListener(SESSION, lambda: None, connect=fc, sleep=make_sleep())
    task = asyncio.create_task(lst.run())
    await asyncio.wait_for(done.wait(), 5)
    await stop(task)
    assert "OSError" in caplog.text
    assert SESSION not in caplog.text


async def test_cancel_closes_socket_and_propagates():
    started = asyncio.Event()

    class Hang(FakeConnect):
        async def __aenter__(self):
            started.set()
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)

    fc = Hang([])
    lst = SsnListener(SESSION, lambda: None, connect=fc, sleep=make_sleep())
    task = asyncio.create_task(lst.run())
    await asyncio.wait_for(started.wait(), 5)
    await stop(task)
    assert fc.closed == 1


def test_url_template_only_uses_keys_it_references():
    default = SsnListener(SESSION, lambda: None, port=1234)
    assert default._url == f"wss://io.socialstream.ninja/join/{SESSION}/4/1"
    local = SsnListener(
        SESSION, lambda: None, url_template="ws://127.0.0.1:{port}/{session}",
        port=99,
    )
    assert local._url == f"ws://127.0.0.1:99/{SESSION}"
    assert SESSION not in local._safe_url
