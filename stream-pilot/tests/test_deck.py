import asyncio
import threading
import types
from io import BytesIO

import pytest
from PIL import Image

from stream_pilot import deck as deck_mod
from stream_pilot.config import DeckCfg
from stream_pilot.deck import StreamDeckAdapter, press_event, render_face
from stream_pilot.events import DeckPress
from stream_pilot.states import STATUS_FACES, KeyFace, State

SIZE = (80, 80)


# ---------------------------------------------------------------- render_face


def test_render_face_size_and_background():
    img = render_face(KeyFace("GO", "#1e9e3e"), SIZE)
    assert img.size == SIZE
    assert img.mode == "RGB"
    assert img.getpixel((1, 1)) == (0x1E, 0x9E, 0x3E)


def test_render_face_draws_label_in_fg():
    img = render_face(KeyFace("LIVE", "#c0141c", fg="#ffffff"), SIZE)
    colors = {c for _, c in img.getcolors(maxcolors=100000)}
    assert (255, 255, 255) in colors or any(
        c[0] > 200 and c[1] > 200 and c[2] > 200 for c in colors
    )


def test_render_face_blink_phase_is_black():
    face = KeyFace("GOING\nLIVE", "#1f5fbf", blink=True)
    on = render_face(face, SIZE, blink_phase=False)
    off = render_face(face, SIZE, blink_phase=True)
    assert on.getpixel((1, 1)) == (0x1F, 0x5F, 0xBF)
    assert off.getpixel((1, 1)) == (0, 0, 0)


def test_render_face_non_blinking_ignores_phase():
    face = KeyFace("LIVE", "#c0141c")
    assert render_face(face, SIZE, blink_phase=True).getpixel((1, 1)) == (0xC0, 0x14, 0x1C)


def _text_bbox(img: Image.Image, bg):
    """Bounding box of every pixel that differs from the background."""
    diff = Image.new("RGB", img.size, bg)
    from PIL import ImageChops

    return ImageChops.difference(img, diff).getbbox()


@pytest.mark.parametrize(
    "label",
    ["RECON-\nNECT", "A VERY LONG LABEL INDEED", "GOING\nLIVE", "SETUP", "X"],
)
def test_render_face_label_fits(label):
    bg = (0x1F, 0x5F, 0xBF)
    img = render_face(KeyFace(label, "#1f5fbf"), SIZE)
    bbox = _text_bbox(img, bg)
    assert bbox is not None
    assert bbox[2] - bbox[0] <= 0.9 * SIZE[0]
    assert bbox[3] - bbox[1] <= SIZE[1]


def test_render_face_all_status_faces_fit():
    for face in STATUS_FACES.values():
        img = render_face(face, SIZE)
        bg = tuple(int(face.bg[i : i + 2], 16) for i in (1, 3, 5))
        bbox = _text_bbox(img, bg)
        assert bbox[2] - bbox[0] <= 0.9 * SIZE[0]


def test_render_face_other_sizes():
    assert render_face(KeyFace("OK", "#000000"), (72, 72)).size == (72, 72)
    assert render_face(KeyFace("OK", "#000000"), (120, 120)).size == (120, 120)


def test_render_face_falls_back_when_no_font_files(monkeypatch):
    monkeypatch.setattr(deck_mod, "FONT_CANDIDATES", ("/nonexistent/a.ttf",))
    deck_mod._font.cache_clear()
    try:
        img = render_face(KeyFace("GO LIVE", "#1e9e3e"), SIZE)
        assert img.size == SIZE
    finally:
        deck_mod._font.cache_clear()


# ---------------------------------------------------------------- press_event


def test_press_event_short():
    assert press_event(2, 10.0, 10.2, 3.0) == DeckPress(2, long=False)


def test_press_event_just_under_threshold():
    assert press_event(0, 10.0, 12.999, 3.0) == DeckPress(0, long=False)


def test_press_event_exactly_threshold_is_long():
    assert press_event(0, 10.0, 13.0, 3.0) == DeckPress(0, long=True)


def test_press_event_over_threshold():
    assert press_event(0, 10.0, 20.0, 3.0) == DeckPress(0, long=True)


# ------------------------------------------------------------------ adapter


class FakeTransportError(Exception):
    pass


class FakePIL:
    """Stands in for StreamDeck.ImageHelpers.PILHelper."""

    @staticmethod
    def to_native_key_format(deck, image):
        buf = BytesIO()
        image.save(buf, "BMP")
        return buf.getvalue()


class FakeDeck:
    def __init__(self, name="deck"):
        self.name = name
        self.lock = threading.RLock()
        self._open = False
        self._connected = True
        self.callback = None
        self.images: dict[int, bytes] = {}
        self.set_image_calls: list[int] = []
        self.calls: list = []
        self.fail_set_image = None  # exception to raise from set_key_image
        self.fail_open = None
        self.closed = 0
        self.close_raises = False

    def __enter__(self):
        self.lock.acquire()

    def __exit__(self, *a):
        self.lock.release()

    def open(self):
        self.calls.append("open")
        if self.fail_open:
            raise self.fail_open
        self._open = True

    def reset(self):
        self.calls.append("reset")

    def set_brightness(self, pct):
        self.calls.append(("brightness", pct))

    def set_key_callback(self, cb):
        self.callback = cb

    def key_image_format(self):
        return {"size": SIZE, "format": "BMP", "flip": (True, True), "rotation": 0}

    def set_key_image(self, key, image):
        if self.fail_set_image:
            raise self.fail_set_image
        self.images[key] = image
        self.set_image_calls.append(key)

    def is_open(self):
        return self._open

    def connected(self):
        return self._connected

    def close(self):
        self.closed += 1
        self._open = False
        if self.close_raises:
            raise FakeTransportError("close failed")


class FakeDeviceManager:
    def __init__(self, decks=None):
        self.decks: list[FakeDeck] = list(decks or [])
        self.enumerate_calls = 0
        self.raises = None

    def enumerate(self):
        self.enumerate_calls += 1
        if self.raises:
            raise self.raises
        return list(self.decks)


def make_adapter(manager, **kw):
    return StreamDeckAdapter(
        DeckCfg(long_press_s=3.0, brightness=40),
        device_manager=manager,
        pil_helper=FakePIL,
        enumerate_interval=0.02,
        blink_interval=0.02,
        **kw,
    )


async def wait_for(pred, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def run_started(adapter):
    """start() returns promptly; the background task is stopped in cleanup."""
    await adapter.start()
    return adapter


@pytest.fixture
async def adapters():
    created = []
    yield created
    for a in created:
        await a.stop()


async def test_start_returns_promptly_without_device(adapters):
    mgr = FakeDeviceManager()
    a = make_adapter(mgr)
    adapters.append(a)
    await asyncio.wait_for(a.start(), 1.0)
    await wait_for(lambda: mgr.enumerate_calls >= 2)  # keeps polling


async def test_show_before_deck_then_rendered_on_connect(adapters):
    deck = FakeDeck()
    mgr = FakeDeviceManager()
    a = make_adapter(mgr)
    adapters.append(a)
    a.show(0, KeyFace("GO LIVE", "#1e9e3e"))
    a.show(4, KeyFace("MEM", "#e67e00"))
    await a.start()
    await wait_for(lambda: mgr.enumerate_calls >= 1)
    assert deck.images == {}
    mgr.decks.append(deck)
    await wait_for(lambda: set(deck.images) >= {0, 4})
    assert deck.calls[:3] == ["open", "reset", ("brightness", 40)]
    img = Image.open(BytesIO(deck.images[0]))
    assert img.size == SIZE
    assert img.getpixel((1, 1)) == (0x1E, 0x9E, 0x3E)


async def test_show_renders_immediately_when_open(adapters):
    deck = FakeDeck()
    a = make_adapter(FakeDeviceManager([deck]))
    adapters.append(a)
    await a.start()
    await wait_for(lambda: deck.is_open() and deck.callback is not None)
    a.show(1, KeyFace("AF", "#1f5fbf"))
    assert 1 in deck.images
    assert Image.open(BytesIO(deck.images[1])).getpixel((1, 1)) == (0x1F, 0x5F, 0xBF)


async def test_key_down_up_from_other_thread_emits_press_on_loop(adapters):
    deck = FakeDeck()
    a = make_adapter(FakeDeviceManager([deck]))
    adapters.append(a)
    seen: list[tuple[object, int]] = []
    loop_thread = threading.get_ident()
    a.set_event_sink(lambda ev: seen.append((ev, threading.get_ident())))
    await a.start()
    await wait_for(lambda: deck.callback is not None)

    def lib_thread():
        deck.callback(deck, 2, True)
        deck.callback(deck, 2, False)

    t = threading.Thread(target=lib_thread)
    t.start()
    t.join()
    await wait_for(lambda: seen)
    assert seen == [(DeckPress(2, long=False), loop_thread)]


async def test_long_press_is_long(adapters, monkeypatch):
    deck = FakeDeck()
    a = make_adapter(FakeDeviceManager([deck]))
    adapters.append(a)
    seen = []
    a.set_event_sink(seen.append)
    await a.start()
    await wait_for(lambda: deck.callback is not None)

    clock = iter([100.0, 103.0])
    # Replace the module's `time` name (not time.monotonic itself: the event
    # loop uses that too).
    monkeypatch.setattr(deck_mod, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))
    deck.callback(deck, 0, True)
    deck.callback(deck, 0, False)
    await wait_for(lambda: seen)
    assert seen == [DeckPress(0, long=True)]


async def test_key_up_without_down_is_ignored(adapters):
    deck = FakeDeck()
    a = make_adapter(FakeDeviceManager([deck]))
    adapters.append(a)
    seen = []
    a.set_event_sink(seen.append)
    await a.start()
    await wait_for(lambda: deck.callback is not None)
    deck.callback(deck, 3, False)
    await asyncio.sleep(0.05)
    assert seen == []


async def test_blink_ticker_alternates_blinking_keys_only(adapters):
    deck = FakeDeck()
    a = make_adapter(FakeDeviceManager([deck]))
    adapters.append(a)
    await a.start()
    await wait_for(lambda: deck.callback is not None)
    a.show(0, KeyFace("GOING\nLIVE", "#1f5fbf", blink=True))
    a.show(2, KeyFace("CAM", "#1e9e3e"))
    seen_pixels = set()
    for _ in range(200):
        await asyncio.sleep(0.01)
        seen_pixels.add(Image.open(BytesIO(deck.images[0])).getpixel((1, 1)))
        if len(seen_pixels) == 2:
            break
    assert seen_pixels == {(0x1F, 0x5F, 0xBF), (0, 0, 0)}
    assert deck.set_image_calls.count(2) == 1  # static key never re-rendered


async def test_transport_error_on_show_reenumerates_and_rerenders(adapters):
    bad = FakeDeck("bad")
    good = FakeDeck("good")
    mgr = FakeDeviceManager([bad])
    a = make_adapter(mgr)
    adapters.append(a)
    await a.start()
    await wait_for(lambda: bad.callback is not None)

    face = KeyFace("LIVE", "#c0141c")
    mgr.decks = [good]
    bad.fail_set_image = FakeTransportError("unplugged")
    bad.close_raises = True  # close errors must be ignored
    a.show(0, face)  # must not raise
    assert bad.closed >= 1
    await wait_for(lambda: 0 in good.images)
    assert good.calls[:3] == ["open", "reset", ("brightness", 40)]
    assert Image.open(BytesIO(good.images[0])).getpixel((1, 1)) == (0xC0, 0x14, 0x1C)


async def test_unplug_detected_by_health_check_reenumerates(adapters):
    d1 = FakeDeck("d1")
    d2 = FakeDeck("d2")
    mgr = FakeDeviceManager([d1])
    a = make_adapter(mgr)
    adapters.append(a)
    a.show(5, KeyFace("JEW", "#1f5fbf"))
    await a.start()
    await wait_for(lambda: 5 in d1.images)
    # The library's reader thread closes the deck itself on a read TransportError.
    mgr.decks = [d2]
    d1._open = False
    await wait_for(lambda: 5 in d2.images)


async def test_open_failure_retries(adapters):
    flaky = FakeDeck()
    flaky.fail_open = FakeTransportError("busy")
    mgr = FakeDeviceManager([flaky])
    a = make_adapter(mgr)
    adapters.append(a)
    a.show(0, KeyFace("X", "#000000"))
    await a.start()
    await wait_for(lambda: mgr.enumerate_calls >= 3)
    assert flaky.images == {}
    flaky.fail_open = None
    await wait_for(lambda: 0 in flaky.images)


async def test_enumerate_exception_is_survived(adapters):
    deck = FakeDeck()
    mgr = FakeDeviceManager([deck])
    mgr.raises = RuntimeError("no libhidapi")
    a = make_adapter(mgr)
    adapters.append(a)
    await a.start()
    await wait_for(lambda: mgr.enumerate_calls >= 2)
    mgr.raises = None
    await wait_for(lambda: deck.callback is not None)


async def test_stale_callback_from_old_deck_ignored(adapters):
    d1, d2 = FakeDeck("d1"), FakeDeck("d2")
    mgr = FakeDeviceManager([d1])
    a = make_adapter(mgr)
    adapters.append(a)
    seen = []
    a.set_event_sink(seen.append)
    await a.start()
    await wait_for(lambda: d1.callback is not None)
    old_cb = d1.callback
    mgr.decks = [d2]
    d1.fail_set_image = FakeTransportError("x")
    a.show(0, KeyFace("A", "#000000"))
    await wait_for(lambda: d2.callback is not None)
    old_cb(d1, 1, True)
    old_cb(d1, 1, False)
    await asyncio.sleep(0.05)
    assert seen == []


async def test_stop_cancels_task_and_closes_deck():
    deck = FakeDeck()
    a = make_adapter(FakeDeviceManager([deck]))
    await a.start()
    await wait_for(lambda: deck.callback is not None)
    await a.stop()
    assert deck.closed >= 1
    assert a._task is None
