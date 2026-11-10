"""Social Stream Ninja chat listener: the second ("ssn") capture source.

SSN (a Chromium extension) relays Facebook comments to a websocket room.
`SsnListener` joins that room and appends every comment to the current
stream's comments.jsonl via `CommentLog.append_ssn`.

`websockets` is imported lazily so this module imports without it. The SSN
session id is a credential: it is never logged (URLs and exception text are
redacted before they reach the log).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import string
import time
from typing import Any, Callable

from .comment_log import Comment, CommentLog

log = logging.getLogger(__name__)

DEFAULT_URL_TEMPLATE = "wss://io.socialstream.ninja/join/{session}/4/1"
_WRAPPER_KEYS = ("msg", "overlayNinja", "data")
_TAG_RE = re.compile(r"<[^>]*>")
_OPEN_TIMEOUT_S = 10
_BACKOFF_MAX_S = 30.0
_STABLE_CONNECTION_S = 30.0


def _clean(value: Any) -> str:
    """Strip HTML tags, unescape entities, collapse whitespace."""
    if not isinstance(value, str):
        return ""
    # Tags first, then entities: an escaped "&lt;3" must survive as "<3".
    return " ".join(html.unescape(_TAG_RE.sub("", value)).split())


def _from_dict(d: dict) -> Comment | None:
    author = _clean(d.get("chatname"))
    text = _clean(d.get("chatmessage"))
    if not author or not text:
        return None
    raw_id = d.get("id")
    raw_ts = d.get("timestamp")
    return Comment(
        author=author,
        text=text,
        fb_comment_id=str(raw_id) if raw_id not in (None, "") else None,
        created_at=str(raw_ts) if raw_ts not in (None, "") else None,
    )


def parse_ssn_message(raw: str) -> Comment | None:
    """Parse one SSN websocket frame; None unless it is a usable comment.

    Accepts the message at top level or under a "msg" / "overlayNinja" /
    "data" wrapper. A JSON list or any non-object payload yields None.
    Never raises.
    """
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return None
        c = _from_dict(obj)
        if c is not None:
            return c
        for key in _WRAPPER_KEYS:
            inner = obj.get(key)
            if isinstance(inner, dict):
                c = _from_dict(inner)
                if c is not None:
                    return c
    except Exception:  # noqa: BLE001 - parsing must never raise
        return None
    return None


def _template_keys(template: str) -> set[str]:
    return {
        name
        for _, name, _, _ in string.Formatter().parse(template)
        if name
    }


class SsnListener:
    """Join an SSN room and append each comment to the current stream's log.

    `log_factory()` returns the current stream's CommentLog, or None when
    not live (messages are then dropped).

    `url_template` is formatted with `session=session_id` plus any extra
    keyword arguments (`**fmt`, e.g. `port=...` for tests that use
    "ws://127.0.0.1:{port}/{session}"). Only the keys the template actually
    uses are passed to `str.format`, so extra keys are harmless.

    `connect` defaults to `websockets.asyncio.client.connect`, resolved
    lazily. `sleep` and `clock` are injectable for tests.
    """

    def __init__(
        self,
        session_id: str,
        log_factory: Callable[[], CommentLog | None],
        url_template: str = DEFAULT_URL_TEMPLATE,
        connect: Callable[..., Any] | None = None,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        **fmt: Any,
    ) -> None:
        self._session_id = session_id
        self._log_factory = log_factory
        self._connect = connect
        self._sleep = sleep
        self._clock = clock
        wanted = _template_keys(url_template)
        values = {"session": session_id, **fmt}
        self._url = url_template.format(
            **{k: v for k, v in values.items() if k in wanted}
        )
        self._safe_url = self._redact(self._url)

    # -- helpers ---------------------------------------------------------

    def _redact(self, text: str) -> str:
        if self._session_id:
            return text.replace(self._session_id, "***")
        return text

    def _resolve_connect(self) -> Callable[..., Any]:
        if self._connect is not None:
            return self._connect
        try:
            from websockets.asyncio.client import connect  # websockets >= 14
        except ImportError:  # pragma: no cover - older websockets
            from websockets import connect  # type: ignore[no-redef]
        return connect

    def _handle(self, raw: Any) -> None:
        try:
            if isinstance(raw, (bytes, bytearray, memoryview)):
                raw = bytes(raw).decode("utf-8", errors="replace")
            c = parse_ssn_message(raw)
            if c is None:
                return
            comment_log = self._log_factory()
            if comment_log is not None:
                comment_log.append_ssn(c)
        except Exception as e:  # noqa: BLE001 - keep the listener alive
            log.warning(
                "ssn: dropped message (%s: %s)",
                type(e).__name__,
                self._redact(str(e)),
            )

    # -- main loop -------------------------------------------------------

    async def run(self) -> None:
        """Connect and consume forever, reconnecting with 1,2,4..30 s backoff.

        Backoff resets to 1 s after a message arrived or the connection
        lasted more than 30 s. CancelledError propagates (the socket is
        closed by the `async with`).
        """
        connect = self._resolve_connect()
        delay = 1.0
        while True:
            started = self._clock()
            got_message = False
            try:
                async with connect(
                    self._url, open_timeout=_OPEN_TIMEOUT_S
                ) as ws:
                    log.info("ssn: connected to %s", self._safe_url)
                    async for raw in ws:
                        got_message = True
                        self._handle(raw)
                log.info("ssn: connection closed (%s)", self._safe_url)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                log.warning(
                    "ssn: connection to %s failed (%s: %s)",
                    self._safe_url,
                    type(e).__name__,
                    self._redact(str(e)),
                )
            if got_message or self._clock() - started > _STABLE_CONNECTION_S:
                delay = 1.0
            await self._sleep(delay)
            delay = min(delay * 2, _BACKOFF_MAX_S)
