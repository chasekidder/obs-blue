"""OBS adapter: ``ObsPort`` over obs-websocket v5 via ``simpleobsws`` 1.4.3.

``simpleobsws`` is imported lazily (only when no client/request factory is
injected), so the core and the unit tests import without it.

Connection behaviour (the controller relies on this):

* ``connect()`` bounds connect + identify by ``connect_timeout_s`` and raises
  ``StepFailed("obs")`` on any failure, never hanging.
* Every request first makes sure the client is identified (so after OBS was
  crashed and restarted by systemd the next request transparently
  reconnects).  If the request itself fails at the transport level
  (closed socket, request timeout) the connection is torn down, reconnected
  exactly once, and the request retried once; a second failure raises
  ``StepFailed("obs")``.
* OBS-level refusals (``not resp.ok()``) raise ``StepFailed("obs")`` too,
  except the idempotent cases ``StartStream`` while already running (500) and
  ``StopStream`` while not running (501), which count as success.

Secrets (websocket password, stream key) are never logged.
"""

import asyncio
import logging
from typing import Any, Callable

from .config import ObsCfg
from .events import ObsStreamState
from .ports import EventSink, StepFailed, StreamTarget

log = logging.getLogger(__name__)

_STATE_MAP = {
    "OBS_WEBSOCKET_OUTPUT_STARTED": ObsStreamState(True, False),
    "OBS_WEBSOCKET_OUTPUT_RECONNECTING": ObsStreamState(True, True),
    "OBS_WEBSOCKET_OUTPUT_RECONNECTED": ObsStreamState(True, False),
    "OBS_WEBSOCKET_OUTPUT_STOPPED": ObsStreamState(False, False),
}

# obs-websocket v5 RequestStatus codes.
_CODE_OUTPUT_RUNNING = 500
_CODE_OUTPUT_NOT_RUNNING = 501
_IDEMPOTENT = {
    "StartStream": _CODE_OUTPUT_RUNNING,
    "StopStream": _CODE_OUTPUT_NOT_RUNNING,
}

_DEFAULT_CONNECT_TIMEOUT_S = 5.0
_DEFAULT_REQUEST_TIMEOUT_S = 10.0
_DISCONNECT_TIMEOUT_S = 2.0


def map_stream_state(event_data: dict | None) -> ObsStreamState | None:
    """Map a ``StreamStateChanged`` eventData dict; ``None`` for transitional
    states (STARTING/STOPPING/...) and malformed data."""
    if not isinstance(event_data, dict):
        return None
    state = event_data.get("outputState")
    if not isinstance(state, str):
        return None
    return _STATE_MAP.get(state)


class SimpleObsClient:
    """``ObsPort`` implementation.

    Injection points (for tests; both default to the real ``simpleobsws``):

    * ``client``: object shaped like ``simpleobsws.WebSocketClient``
      (``connect``, ``wait_until_identified``, ``disconnect``, ``call``,
      ``register_event_callback``, ``identified``).
    * ``request_factory``: ``(request_type, request_data) -> request`` used
      instead of ``simpleobsws.Request``.
    """

    def __init__(
        self,
        cfg: ObsCfg,
        *,
        client: Any = None,
        request_factory: Callable[[str, dict | None], Any] | None = None,
        connect_timeout_s: float = _DEFAULT_CONNECT_TIMEOUT_S,
        request_timeout_s: float = _DEFAULT_REQUEST_TIMEOUT_S,
    ):
        self._cfg = cfg
        self._client = client
        self._request_factory = request_factory
        self._connect_timeout_s = connect_timeout_s
        self._request_timeout_s = request_timeout_s
        self._sink: EventSink | None = None
        self._callback_registered = False
        self._lock = asyncio.Lock()

    # -- ObsPort -----------------------------------------------------------

    def set_event_sink(self, sink: EventSink) -> None:
        self._sink = sink

    async def connect(self) -> None:
        await self._ensure_connected()

    async def stream_active(self) -> bool:
        data = await self._request("GetStreamStatus")
        if not isinstance(data, dict) or "outputActive" not in data:
            raise StepFailed("obs", "GetStreamStatus: no outputActive in response")
        return bool(data["outputActive"])

    async def set_stream_target(self, target: StreamTarget) -> None:
        await self._request(
            "SetStreamServiceSettings",
            {
                "streamServiceType": "rtmp_custom",
                "streamServiceSettings": {
                    "server": target.server,
                    "key": target.key,
                },
            },
        )

    async def start_stream(self) -> None:
        await self._request("StartStream")

    async def stop_stream(self) -> None:
        await self._request("StopStream")

    async def set_scene(self, name: str) -> None:
        await self._request("SetCurrentProgramScene", {"sceneName": name})

    async def current_scene(self) -> str:
        data = await self._request("GetCurrentProgramScene")
        if isinstance(data, dict):
            name = data.get("currentProgramSceneName", data.get("sceneName"))
            if isinstance(name, str):
                return name
        raise StepFailed("obs", "GetCurrentProgramScene: no scene name in response")

    # -- internals ---------------------------------------------------------

    def _get_client(self) -> Any:
        if self._client is None:
            import simpleobsws  # lazy: not needed by the core or unit tests

            self._client = simpleobsws.WebSocketClient(
                url=f"ws://{self._cfg.host}:{self._cfg.port}",
                password=self._cfg.password,
            )
        if not self._callback_registered:
            self._client.register_event_callback(self._on_stream_state, "StreamStateChanged")
            self._callback_registered = True
        return self._client

    def _make_request(self, request_type: str, data: dict | None) -> Any:
        if self._request_factory is not None:
            return self._request_factory(request_type, data)
        import simpleobsws

        return simpleobsws.Request(request_type, data)

    async def _on_stream_state(self, event_data: dict | None) -> None:
        # simpleobsws passes eventData when registered for a specific event.
        ev = map_stream_state(event_data)
        if ev is None or self._sink is None:
            return
        try:
            self._sink(ev)
        except Exception:
            log.exception("obs: event sink raised while handling %r", ev)

    async def _ensure_connected(self) -> None:
        client = self._get_client()
        async with self._lock:
            if getattr(client, "identified", False):
                return
            # Drop any half-open session (e.g. socket open but never
            # identified, or a stale one) before dialing again.
            await self._disconnect_quietly(client)
            try:
                await asyncio.wait_for(
                    self._dial(client), timeout=self._connect_timeout_s
                )
            except StepFailed:
                await self._disconnect_quietly(client)
                raise
            except asyncio.TimeoutError:
                await self._disconnect_quietly(client)
                raise StepFailed(
                    "obs", f"connect timed out after {self._connect_timeout_s:g}s"
                ) from None
            except Exception as e:
                await self._disconnect_quietly(client)
                raise StepFailed("obs", f"connect failed: {type(e).__name__}: {e}") from e

    async def _dial(self, client: Any) -> None:
        await client.connect()
        if not await client.wait_until_identified(timeout=self._connect_timeout_s):
            raise StepFailed("obs", "identify failed (auth rejected or OBS not ready)")

    async def _disconnect_quietly(self, client: Any) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=_DISCONNECT_TIMEOUT_S)
        except Exception:  # includes TimeoutError; nothing useful to do
            pass

    async def _request(self, request_type: str, data: dict | None = None) -> dict | None:
        """Send one request; reconnect+retry once on transport failure."""
        last: Exception | None = None
        for attempt in (0, 1):
            await self._ensure_connected()  # raises StepFailed("obs")
            client = self._get_client()
            try:
                resp = await client.call(
                    self._make_request(request_type, data),
                    timeout=self._request_timeout_s,
                )
            except Exception as e:  # closed socket, MessageTimeout, NotIdentified...
                last = e
                log.warning(
                    "obs: %s failed (%s: %s)%s",
                    request_type,
                    type(e).__name__,
                    e,
                    "; reconnecting once" if attempt == 0 else "",
                )
                await self._disconnect_quietly(client)
                continue
            return self._check(request_type, resp)
        raise StepFailed(
            "obs", f"{request_type}: {type(last).__name__}: {last}"
        ) from last

    def _check(self, request_type: str, resp: Any) -> dict | None:
        if resp.ok():
            return resp.responseData
        status = resp.requestStatus
        if _IDEMPOTENT.get(request_type) == status.code:
            log.info(
                "obs: %s already in requested state (%s %s); treating as success",
                request_type,
                status.code,
                status.comment,
            )
            return resp.responseData
        raise StepFailed("obs", f"{request_type}: {status.code} {status.comment}")
