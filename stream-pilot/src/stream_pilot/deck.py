"""Elgato Stream Deck Mini adapter (``DeckPort``).

The ``streamdeck`` package (python-elgato-streamdeck) is imported lazily so the
core and the unit tests import without it.

Threading model
---------------
* Everything that talks to the deck (``open``/``reset``/``set_key_image``/
  ``close``) runs on the asyncio loop thread.  Those calls are short blocking
  HID writes (a few ms for an 80x80 BMP); they are done inline, under the
  library's own ``with deck:`` update lock, rather than through a thread pool.
* The library's reader thread only runs our key callback, which records the
  ``time.monotonic()`` of a key-down and, on key-up, hands a ``DeckPress`` to
  the loop via ``loop.call_soon_threadsafe``.  It never touches the deck.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from functools import lru_cache
from itertools import combinations
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .config import DeckCfg
from .events import DeckDown, DeckPress, Event
from .ports import EventSink
from .states import KeyFace

log = logging.getLogger(__name__)

FONT_CANDIDATES = (
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

_MIN_FONT_SIZE = 6
_FIT_WIDTH = 0.9


@lru_cache(maxsize=128)
def _font(size: int) -> Any:
    import os

    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _hex_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _wrapped(label: str, lines: int) -> str:
    """Split a one-line label at spaces into ``lines`` lines of balanced length."""
    words = label.split()
    best: list[str] | None = None
    for cuts in combinations(range(1, len(words)), lines - 1):
        bounds = (0, *cuts, len(words))
        rows = [" ".join(words[a:b]) for a, b in zip(bounds, bounds[1:])]
        if best is None or max(map(len, rows)) < max(map(len, best)):
            best = rows
    return "\n".join(best or [label])


def render_face(
    face: KeyFace, size: tuple[int, int], blink_phase: bool = False
) -> Image.Image:
    """Render a key face: solid ``bg`` (black in the blink-off phase) with the
    multiline label centred in ``fg``, shrunk until it fits 90% of the width.

    A one-line label too wide even at the minimum size is wrapped at spaces
    instead (2-4 balanced lines, largest size that fits)."""
    width, height = size
    black = face.blink and blink_phase
    img = Image.new("RGB", size, (0, 0, 0) if black else _hex_rgb(face.bg))
    if not face.label:
        return img
    draw = ImageDraw.Draw(img)
    centre = (width / 2, height / 2)
    start = max(_MIN_FONT_SIZE, int(height * 0.3))

    def fits(text: str, px: int) -> bool:
        left, top, right, bottom = draw.multiline_textbbox(
            centre, text, font=_font(px), anchor="mm", align="center"
        )
        return right - left <= _FIT_WIDTH * width and bottom - top <= height

    text, px = face.label, start
    while px > _MIN_FONT_SIZE and not fits(text, px):
        px -= 1
    words = face.label.split()
    if not fits(text, px) and "\n" not in face.label and len(words) > 1:
        layouts = [_wrapped(face.label, n) for n in range(2, min(len(words), 4) + 1)]
        text, px = next(
            ((t, p) for p in range(start, _MIN_FONT_SIZE - 1, -1) for t in layouts if fits(t, p)),
            (layouts[-1], _MIN_FONT_SIZE),
        )
    draw.multiline_text(
        centre, text, font=_font(px), fill=_hex_rgb(face.fg), anchor="mm", align="center"
    )
    return img


def press_event(key: int, down_at: float, up_at: float, long_press_s: float) -> DeckPress:
    """Pure press-duration logic: held for >= ``long_press_s`` is a long press."""
    return DeckPress(key, long=(up_at - down_at) >= long_press_s)


class StreamDeckAdapter:
    """``DeckPort`` backed by a USB Stream Deck (hotplug tolerant)."""

    def __init__(
        self,
        cfg: DeckCfg,
        *,
        device_manager: Any = None,
        pil_helper: Any = None,
        enumerate_interval: float = 5.0,
        blink_interval: float = 0.5,
    ):
        self._cfg = cfg
        self._manager = device_manager  # injected for tests; lazy otherwise
        self._pil = pil_helper
        self._enumerate_interval = enumerate_interval
        self._blink_interval = blink_interval
        self._sink: EventSink | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._deck: Any = None
        self._faces: dict[int, KeyFace] = {}
        self._phase = False
        self._down: dict[int, float] = {}
        self._last_error = ""

    # ------------------------------------------------------------- DeckPort

    def set_event_sink(self, sink: EventSink) -> None:
        self._sink = sink

    def show(self, key: int, face: KeyFace) -> None:
        self._faces[key] = face
        if self._deck is not None:
            self._render_key(key, blink_phase=False)  # start lit, not mid-"off"

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="deck-adapter")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._drop("stopping")

    # ------------------------------------------------------------ internals

    def _pil_helper(self) -> Any:
        if self._pil is None:
            from StreamDeck.ImageHelpers import PILHelper

            self._pil = PILHelper
        return self._pil

    def _device_manager(self) -> Any:
        if self._manager is None:
            from StreamDeck.DeviceManager import DeviceManager

            self._manager = DeviceManager()
        return self._manager

    def _note_error(self, what: str, exc: BaseException) -> None:
        msg = f"{what}: {type(exc).__name__}: {exc}"
        if msg != self._last_error:  # don't spam the journal every 5 s
            self._last_error = msg
            log.warning("stream deck %s", msg)

    async def _run(self) -> None:
        while True:
            if self._deck is None:
                self._try_connect()
                if self._deck is None:  # only back off while still no deck
                    await asyncio.sleep(self._enumerate_interval)
            else:
                await asyncio.sleep(self._blink_interval)
                self._tick()

    def _try_connect(self) -> None:
        try:
            decks = self._device_manager().enumerate()
        except Exception as exc:  # missing libhidapi/streamdeck, USB errors
            self._note_error("enumerate failed", exc)
            return
        if not decks:
            return
        deck = decks[0]
        try:
            deck.open()
            deck.reset()
            deck.set_brightness(self._cfg.brightness)
            deck.set_key_callback(self._make_callback(deck))
        except Exception as exc:
            self._note_error("open failed", exc)
            self._close_quietly(deck)
            return
        self._deck = deck
        self._down.clear()
        self._phase = False
        log.info("stream deck connected")
        for key in sorted(self._faces):  # remembered faces
            if self._deck is not deck:  # a render failed and dropped it
                break
            self._render_key(key)
        if self._deck is deck:  # full render succeeded: re-arm error logging
            self._last_error = ""

    def _tick(self) -> None:
        deck = self._deck
        if deck is None:
            return
        try:
            # The library's reader thread closes the deck on a read TransportError
            # (unplug), so is_open() suffices. Not connected(): in 0.10.0 that
            # does a full unfiltered USB enumerate.
            gone = not deck.is_open()
        except Exception:
            gone = True
        if gone:
            self._drop("device went away")
            return
        self._phase = not self._phase
        for key, face in sorted(self._faces.items()):
            if face.blink and self._deck is deck:
                self._render_key(key)

    def _render_key(self, key: int, blink_phase: bool | None = None) -> None:
        deck = self._deck
        face = self._faces.get(key)
        if deck is None or face is None:
            return
        try:
            with deck:
                size = deck.key_image_format()["size"]
                img = render_face(
                    face, size, self._phase if blink_phase is None else blink_phase
                )
                deck.set_key_image(key, self._pil_helper().to_native_key_format(deck, img))
        except Exception as exc:  # TransportError / unplug / bad key index
            self._note_error(f"render key {key} failed", exc)
            self._drop("transport error")

    def _drop(self, why: str) -> None:
        deck, self._deck = self._deck, None
        self._down.clear()
        if deck is not None:
            log.info("stream deck closed (%s)", why)
            self._close_quietly(deck)

    @staticmethod
    def _close_quietly(deck: Any) -> None:
        with contextlib.suppress(Exception):
            deck.close()

    def _make_callback(self, deck: Any):
        def on_key(_deck: Any, key: int, pressed: bool) -> None:
            # Runs on the library's reader thread: compute times, hand off.
            if deck is not self._deck:
                return  # stale callback from a dropped/replaced deck
            now = time.monotonic()
            loop = self._loop
            if pressed:
                self._down[key] = now
                if loop is not None:
                    with contextlib.suppress(RuntimeError):
                        loop.call_soon_threadsafe(self._deliver, DeckDown(key))
                return
            down_at = self._down.pop(key, None)
            if down_at is None or loop is None:
                return
            event = press_event(key, down_at, now, self._cfg.long_press_s)
            with contextlib.suppress(RuntimeError):  # loop already closed
                loop.call_soon_threadsafe(self._deliver, event)

        return on_key

    def _deliver(self, event: Event) -> None:
        if self._sink is not None:
            self._sink(event)
