"""Host (no browser) tests for fb/comments.py: per-context router and the binding callback."""

from __future__ import annotations

import json
import logging

import pytest

from stream_pilot.comment_log import CommentLog
from stream_pilot.fb.comments import BINDING_NAME, FbDomCapture

LIVE_ID = "1122334455667788"


class FakePage:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.reloads = 0

    async def reload(self) -> None:
        self.reloads += 1


class FakeContext:
    """Mimics Playwright: registering the same binding twice raises."""

    def __init__(self) -> None:
        self.bindings: dict[str, object] = {}
        self.scripts: list[str] = []

    async def expose_binding(self, name, callback) -> None:
        if name in self.bindings:
            raise RuntimeError(f"Function {name!r} has been already registered")
        self.bindings[name] = callback

    async def add_init_script(self, script) -> None:
        self.scripts.append(script)

    def send(self, page, payload) -> None:
        self.bindings[BINDING_NAME]({"page": page}, payload)


def batch(kind: str, *texts: str) -> str:
    return json.dumps({"kind": kind, "items": [{"author": "Ann", "text": t} for t in texts]})


def lines(log: CommentLog) -> list[dict]:
    if not log.path.exists():
        return []
    return [json.loads(x) for x in log.path.read_text().splitlines() if x.strip()]


@pytest.fixture
def log(tmp_path) -> CommentLog:
    return CommentLog(tmp_path / "c.jsonl", LIVE_ID)


# -- I1: one router per context ------------------------------------------------


async def test_two_captures_on_one_context_install_binding_and_script_once(log, tmp_path):
    ctx = FakeContext()
    page = FakePage()
    log2 = CommentLog(tmp_path / "c2.jsonl", LIVE_ID)
    first, second = FbDomCapture(ctx), FbDomCapture(ctx)
    await first.attach(page, log)
    await second.attach(page, log2)  # must not raise "already registered"
    assert list(ctx.bindings) == [BINDING_NAME]
    assert len(ctx.scripts) == 1


async def test_second_capture_captures_and_replaces_first_routing(log, tmp_path):
    ctx = FakeContext()
    page = FakePage()
    log2 = CommentLog(tmp_path / "c2.jsonl", LIVE_ID)
    first, second = FbDomCapture(ctx), FbDomCapture(ctx)
    await first.attach(page, log)
    ctx.send(page, batch("live", "one"))
    assert first.counts["live"] == 1
    await second.attach(page, log2)
    assert first.attached_page is None and second.attached_page is page
    ctx.send(page, batch("live", "two"))
    assert [r["text"] for r in lines(log)] == ["one"]
    assert [r["text"] for r in lines(log2)] == ["two"]
    assert second.counts["live"] == 1 and first.counts["live"] == 1
    assert second.last_message_at is not None


async def test_stale_handle_detach_does_not_stop_the_current_handle(log, tmp_path):
    ctx = FakeContext()
    page = FakePage()
    log2 = CommentLog(tmp_path / "c2.jsonl", LIVE_ID)
    first, second = FbDomCapture(ctx), FbDomCapture(ctx)
    await first.attach(page, log)
    await second.attach(page, log2)
    await first.detach()  # superseded handle: must not cut off `second`
    assert second.attached_page is page
    ctx.send(page, batch("live", "still routed"))
    assert [r["text"] for r in lines(log2)] == ["still routed"]


async def test_reattach_same_handle_and_detach(log):
    ctx = FakeContext()
    page = FakePage()
    cap = FbDomCapture(ctx)
    await cap.attach(page, log)
    await cap.attach(page, log)
    assert len(ctx.scripts) == 1
    await cap.detach()
    assert cap.attached_page is None
    ctx.send(page, batch("live", "dropped"))
    assert lines(log) == []


async def test_different_contexts_get_their_own_binding(log):
    a, b = FakeContext(), FakeContext()
    await FbDomCapture(a).attach(FakePage(), log)
    await FbDomCapture(b).attach(FakePage(), log)
    assert BINDING_NAME in a.bindings and BINDING_NAME in b.bindings


async def test_loaded_page_is_reloaded_blank_page_is_not(log):
    ctx = FakeContext()
    blank, loaded = FakePage(), FakePage("http://x/live")
    cap = FbDomCapture(ctx)
    await cap.attach(blank, log)
    await cap.attach(loaded, log)
    assert (blank.reloads, loaded.reloads) == (0, 1)


# -- M6: the binding callback never raises --------------------------------------


@pytest.fixture
async def attached(log):
    ctx = FakeContext()
    page = FakePage()
    cap = FbDomCapture(ctx)
    await cap.attach(page, log)
    return ctx, page, cap


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "",
        "{",
        None,
        12,
        b"\xff\xfe",
        "[]",
        "null",
        '"just a string"',
        "42",
        '{"kind": "rescan", "items": "oops"}',
        '{"kind": "live", "items": 5}',
        '{"kind": "live", "items": {"author": "x"}}',
    ],
)
async def test_malformed_payloads_are_logged_not_raised_and_not_ingested(attached, log, caplog, payload):
    ctx, page, cap = attached
    with caplog.at_level(logging.WARNING, logger="stream_pilot.fb.comments"):
        ctx.send(page, payload)  # must not raise
    assert lines(log) == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records), "malformed input must be logged"


async def test_non_dict_items_are_skipped_and_nothing_raises(attached, log):
    ctx, page, cap = attached
    ctx.send(page, json.dumps({"kind": "live", "items": [1, None, "x", ["a"], {"author": "Ann", "text": "ok"}]}))
    assert [r["text"] for r in lines(log)] == ["ok"]


async def test_unknown_kind_is_ignored(attached, log):
    ctx, page, cap = attached
    ctx.send(page, json.dumps({"kind": "bogus", "items": [{"author": "A", "text": "t"}]}))
    ctx.send(page, json.dumps({"items": [{"author": "A", "text": "t"}]}))
    assert lines(log) == []


async def test_source_without_page_is_logged_not_raised(attached, log, caplog):
    ctx, page, cap = attached
    cb = ctx.bindings[BINDING_NAME]
    with caplog.at_level(logging.WARNING, logger="stream_pilot.fb.comments"):
        cb({}, batch("live", "x"))
        cb(None, batch("live", "x"))
    assert lines(log) == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_payload_from_an_unregistered_page_is_dropped(attached, log):
    ctx, page, cap = attached
    other = FakePage()
    ctx.send(other, batch("live", "from elsewhere"))
    ctx.send(other, batch("rescan", "from elsewhere"))
    assert lines(log) == []
    assert cap.counts == {} and cap.last_message_at is None


async def test_exception_inside_the_log_is_logged_not_raised(attached, caplog):
    ctx, page, cap = attached

    class Boom:
        def ingest_dom(self, items, *, rescan):
            raise OSError("disk full")

    await cap.attach(page, Boom())  # type: ignore[arg-type]
    with caplog.at_level(logging.ERROR, logger="stream_pilot.fb.comments"):
        ctx.send(page, batch("live", "x"))
    assert any("disk full" in (r.exc_text or "") or r.exc_info for r in caplog.records)


async def test_valid_messages_are_counted_and_stamped(attached, log):
    ctx, page, cap = attached
    ticks = iter([5.0, 6.0, 7.0])
    cap._clock = lambda: next(ticks)  # noqa: SLF001 - clock injection point
    ctx.send(page, json.dumps({"kind": "hello", "href": "http://x/"}))
    ctx.send(page, batch("rescan", "a"))
    ctx.send(page, batch("live", "b"))
    assert dict(cap.counts) == {"hello": 1, "rescan": 1, "live": 1}
    assert cap.last_message_at == 7.0
    assert [r["text"] for r in lines(log)] == ["a", "b"]
