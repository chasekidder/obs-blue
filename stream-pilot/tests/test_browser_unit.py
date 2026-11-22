"""Host-side unit tests for ``browser.py`` and ``fb/adapter.py`` (no Playwright, no Chromium).

Playwright objects are replaced by tiny fakes; HTTP checks use a local
``http.server`` on 127.0.0.1 (never a real site).
"""

from __future__ import annotations

import asyncio
import dataclasses
import http.server
import json
import logging
import threading
from pathlib import Path

import pytest

from stream_pilot import browser as browser_mod
from stream_pilot.browser import (
    SSN_EXTENSION_ID,
    BrowserNotConnected,
    CdpBrowser,
    ssn_helper_url,
)
from stream_pilot.comment_log import CommentLog
from stream_pilot.config import load_config
from stream_pilot.fb import adapter as adapter_mod
from stream_pilot.fb import selectors as S
from stream_pilot.fb.adapter import CaptureAdapter, FacebookAdapter
from stream_pilot.ports import StepFailed

LIVE_ID = "1122334455667788"


# ----------------------------------------------------------------- fakes


class FakePage:
    def __init__(self, url: str = "about:blank"):
        self.url = url
        self._closed = False
        self.closes = 0
        self.close_hangs = False
        self.gotos: list[str] = []

    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self.closes += 1
        if self.close_hangs:
            await asyncio.sleep(30)
        self._closed = True

    async def goto(self, url: str, **kw) -> None:
        self.url = url
        self.gotos.append(url)


class FakeSession:
    def __init__(self, owner: "FakeBrowser"):
        self.owner = owner
        self.calls: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict | None = None):
        self.calls.append((method, dict(params or {})))
        hook = self.owner.on_send
        if hook is not None:
            await hook(method, params or {})
        if method in self.owner.send_raises:
            raise self.owner.send_raises[method]
        if method == "Target.getTargets":
            return {"targetInfos": list(self.owner.targets)}
        return {"targetId": "T1"}

    async def detach(self) -> None:
        pass


class FakeContext:
    def __init__(self, pages: list[FakePage] | None = None):
        self.pages: list[FakePage] = pages if pages is not None else []
        self.new_pages = 0

    async def new_page(self) -> FakePage:
        self.new_pages += 1
        page = FakePage()
        self.pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, context: FakeContext):
        self.context = context
        self.connected = True
        self.sessions: list[FakeSession] = []
        self.on_send = None
        self.send_raises: dict[str, Exception] = {}
        # CDP Target.getTargets: unlike context.pages, it lists chrome-extension:// tabs
        self.targets: list[dict] = []

    def is_connected(self) -> bool:
        return self.connected

    async def new_browser_cdp_session(self) -> FakeSession:
        s = FakeSession(self)
        self.sessions.append(s)
        return s

    @property
    def calls(self) -> list[tuple[str, dict]]:
        return [c for s in self.sessions for c in s.calls]


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def cfg(tmp_path):
    site = tmp_path / "site.toml"
    site.write_text(
        f"""
[facebook]
actor_id = "111"
group_id = "222"
group_name = "Test Group"
producer_url_template = "http://127.0.0.1:1/live/producer/?actor={{actor_id}}&target_id={{group_id}}"
comments_url_template = "http://127.0.0.1:1/live/producer/dashboard/{{live_id}}/COMMENTS/"

[cdp]
user_data_dir = "{tmp_path.as_posix()}/profile"
chromium_bin = "/usr/bin/test-chromium"

[ssn]
enabled = false
session_id = ""

[timeouts]
step_s = 0.3
"""
    )
    return load_config([site])


def make_browser(cfg, pages=None, *, spawn=None) -> tuple[CdpBrowser, FakeContext, FakeBrowser]:
    ctx = FakeContext(pages)
    fb = FakeBrowser(ctx)
    b = CdpBrowser(cfg.cdp, spawn=spawn)
    b.poll_s = 0.005
    b.fallback_wait_s = 0.2
    b.helper_wait_s = 0.05
    b._bind(FakePlaywright(), fb, ctx)
    return b, ctx, fb


# ----------------------------------------------------- SSN manifest lookup


def write_manifest(root: Path, version: str, manifest: dict | str, files=()) -> Path:
    d = root / "Default" / "Extensions" / SSN_EXTENSION_ID / version
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(manifest if isinstance(manifest, str) else json.dumps(manifest))
    for f in files:
        (d / f).parent.mkdir(parents=True, exist_ok=True)
        (d / f).write_text("<html></html>")
    return d


def test_ssn_extension_constants():
    assert SSN_EXTENSION_ID == "cppibjhfemifednoimlblfcmjgfhfjeg"


def test_ssn_helper_absent_extension(tmp_path):
    assert ssn_helper_url(tmp_path) is None
    assert ssn_helper_url(tmp_path / "does-not-exist") is None


def test_ssn_helper_uses_action_default_popup(tmp_path):
    write_manifest(tmp_path, "3.1_0", {"manifest_version": 3, "action": {"default_popup": "menu.html"}})
    assert ssn_helper_url(tmp_path) == f"chrome-extension://{SSN_EXTENSION_ID}/menu.html"


def test_ssn_helper_uses_browser_action_popup_and_normalizes_path(tmp_path):
    write_manifest(tmp_path, "1_0", {"browser_action": {"default_popup": "./ui/popup.html?x=1"}})
    assert ssn_helper_url(tmp_path) == f"chrome-extension://{SSN_EXTENSION_ID}/ui/popup.html?x=1"


def test_ssn_helper_falls_back_to_popup_html_only_if_the_file_exists(tmp_path):
    d = write_manifest(tmp_path, "1_0", {"manifest_version": 3, "name": "x"})
    assert ssn_helper_url(tmp_path) is None
    (d / "popup.html").write_text("<html></html>")
    assert ssn_helper_url(tmp_path) == f"chrome-extension://{SSN_EXTENSION_ID}/popup.html"


def test_ssn_helper_picks_the_newest_version(tmp_path):
    write_manifest(tmp_path, "1.9_0", {"action": {"default_popup": "old.html"}})
    write_manifest(tmp_path, "1.10_0", {"action": {"default_popup": "new.html"}})
    assert ssn_helper_url(tmp_path) == f"chrome-extension://{SSN_EXTENSION_ID}/new.html"


@pytest.mark.parametrize("bad", ["{not json", "[]", '"x"', json.dumps({"action": {"default_popup": 5}})])
def test_ssn_helper_ignores_broken_manifests(tmp_path, bad):
    write_manifest(tmp_path, "1_0", bad)
    assert ssn_helper_url(tmp_path) is None


def test_ssn_helper_rejects_popup_paths_escaping_the_extension(tmp_path):
    write_manifest(tmp_path, "1_0", {"action": {"default_popup": "../../evil.html"}})
    assert ssn_helper_url(tmp_path) is None


# ------------------------------------------------------- http_reachable


class _Server:
    def __init__(self):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.paths.append(self.path)
                if self.path == "/slow":
                    threading.Event().wait(2)
                code = int(self.path.strip("/") or 200)
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_HEAD = do_GET

            def log_message(self, *a):
                pass

        self.paths: list[str] = []
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.close()


@pytest.mark.parametrize("status, expected", [(200, True), (302, True), (404, True), (499, True), (500, False), (503, False)])
async def test_http_reachable_by_status(cfg, server, status, expected):
    b = CdpBrowser(cfg.cdp)
    assert await b.http_reachable(f"{server.base}/{status}") is expected


async def test_http_reachable_false_when_nothing_listens(cfg):
    s = _Server()
    base = s.base
    s.close()
    b = CdpBrowser(cfg.cdp)
    assert await b.http_reachable(base, timeout_s=1) is False


async def test_http_reachable_false_on_timeout(cfg, server):
    b = CdpBrowser(cfg.cdp)
    assert await b.http_reachable(f"{server.base}/slow", timeout_s=0.3) is False


async def test_http_reachable_does_not_need_a_connection(cfg, server):
    b = CdpBrowser(cfg.cdp)
    assert not b.is_connected
    assert await b.http_reachable(server.base + "/200") is True


# ------------------------------------------------------ CdpBrowser pages


async def test_unconnected_browser_raises_clear_error(cfg):
    b = CdpBrowser(cfg.cdp)
    assert not b.is_connected
    with pytest.raises(BrowserNotConnected, match="not connected"):
        _ = b.context
    with pytest.raises(BrowserNotConnected):
        await b.dashboard_page()
    with pytest.raises(BrowserNotConnected):
        await b.open_app_window("http://x.test/a/")
    with pytest.raises(BrowserNotConnected):
        await b.find_page("http://x.test/")
    assert isinstance(BrowserNotConnected("x"), RuntimeError)


async def test_disconnected_browser_counts_as_not_connected(cfg):
    b, _, fb = make_browser(cfg)
    assert b.is_connected
    fb.connected = False
    assert not b.is_connected
    with pytest.raises(BrowserNotConnected):
        await b.dashboard_page()


async def test_dashboard_page_skips_extension_devtools_and_popout_pages(cfg):
    ext = FakePage(f"chrome-extension://{SSN_EXTENSION_ID}/popup.html")
    dev = FakePage("devtools://devtools/bundled/inspector.html")
    popout = FakePage("http://x.test/live/producer/dashboard/1/COMMENTS/")
    main = FakePage("about:blank")
    b, ctx, _ = make_browser(cfg, [ext, dev, popout, main])
    b.track_popout(popout)
    assert await b.dashboard_page() is main
    assert ctx.new_pages == 0


async def test_dashboard_page_creates_one_when_only_helper_pages_exist(cfg):
    ext = FakePage(f"chrome-extension://{SSN_EXTENSION_ID}/popup.html")
    b, ctx, _ = make_browser(cfg, [ext])
    page = await b.dashboard_page()
    assert ctx.new_pages == 1 and page in ctx.pages and page is not ext
    assert await b.dashboard_page() is page  # stable afterwards


async def test_dashboard_page_ignores_closed_pages(cfg):
    dead = FakePage("http://x.test/")
    await dead.close()
    b, ctx, _ = make_browser(cfg, [dead])
    page = await b.dashboard_page()
    assert page is not dead


async def test_find_page_prefix_and_contains(cfg):
    a = FakePage("http://x.test/live/producer/dashboard/1122/COMMENTS/?x=1")
    b_, ctx, _ = make_browser(cfg, [FakePage("about:blank"), a])
    assert await b_.find_page("http://x.test/live/producer/dashboard/1122/COMMENTS") is a
    assert await b_.find_page("http://x.test/other") is None
    assert await b_.find_page(contains="/1122/COMMENTS") is a
    assert await b_.find_page(contains="/9999/COMMENTS") is None
    await a.close()
    assert await b_.find_page(contains="/1122/COMMENTS") is None


# ---------------------------------------------------- open_app_window

URL = "http://x.test/live/producer/dashboard/1122/COMMENTS/?ref=1"
PREFIX = "http://x.test/live/producer/dashboard/1122/COMMENTS/"


async def test_open_app_window_spawns_chromium_and_waits_for_the_page(cfg):
    spawned: list[list[str]] = []
    holder: dict = {}

    async def spawn(argv):
        spawned.append(argv)

        async def later():
            await asyncio.sleep(0.03)
            holder["ctx"].pages.append(FakePage("about:blank"))  # opens blank first
            await asyncio.sleep(0.03)
            holder["ctx"].pages[-1].url = URL

        holder["task"] = asyncio.ensure_future(later())

    b, ctx, fb = make_browser(cfg, spawn=spawn)
    holder["ctx"] = ctx
    page = await b.open_app_window(URL, timeout_s=2)
    await holder["task"]
    assert page.url == URL
    assert spawned == [
        ["/usr/bin/test-chromium", f"--user-data-dir={cfg.cdp.user_data_dir}", f"--app={URL}"]
    ]
    assert fb.calls == []  # no CDP fallback needed
    assert page in b.popout_pages


async def test_open_app_window_returns_an_already_open_page_without_spawning(cfg):
    spawn_calls = []

    async def spawn(argv):
        spawn_calls.append(argv)

    existing = FakePage(URL)
    b, _, _ = make_browser(cfg, [existing], spawn=spawn)
    assert await b.open_app_window(URL) is existing
    assert spawn_calls == []
    assert existing in b.popout_pages


async def test_open_app_window_falls_back_to_cdp_new_window(cfg, caplog):
    async def spawn(argv):
        pass  # handoff never produces a page

    b, ctx, fb = make_browser(cfg, spawn=spawn)

    async def on_send(method, params):
        if method == "Target.createTarget":
            ctx.pages.append(FakePage(params["url"]))

    fb.on_send = on_send
    with caplog.at_level(logging.WARNING):
        page = await b.open_app_window(URL, timeout_s=0.05)
    assert page.url == URL
    assert ("Target.createTarget", {"url": URL, "newWindow": True}) in fb.calls
    assert "falling back" in caplog.text.lower()
    assert page in b.popout_pages


async def test_open_app_window_falls_back_at_once_when_the_handoff_process_failed(cfg, caplog):
    async def spawn(argv):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(21)  # chromium exit code "profile in use": the running instance refused
        return fut

    b, ctx, fb = make_browser(cfg, spawn=spawn)

    async def on_send(method, params):
        ctx.pages.append(FakePage(params["url"]))

    fb.on_send = on_send
    started = asyncio.get_running_loop().time()
    page = await b.open_app_window(URL, timeout_s=30)
    assert asyncio.get_running_loop().time() - started < 2
    assert page.url == URL


async def test_open_app_window_keeps_waiting_when_the_handoff_process_exited_zero(cfg):
    holder = {}

    async def spawn(argv):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(0)  # handed off and exited; the window may still be coming

        async def later():
            await asyncio.sleep(0.1)
            holder["ctx"].pages.append(FakePage(URL))

        asyncio.ensure_future(later())
        return fut

    b, ctx, fb = make_browser(cfg, spawn=spawn)
    holder["ctx"] = ctx
    page = await b.open_app_window(URL, timeout_s=2)
    assert page.url == URL
    assert fb.calls == []


async def test_open_app_window_spawn_error_also_falls_back(cfg):
    async def spawn(argv):
        raise FileNotFoundError("no chromium")

    b, ctx, fb = make_browser(cfg, spawn=spawn)

    async def on_send(method, params):
        ctx.pages.append(FakePage(params["url"]))

    fb.on_send = on_send
    page = await b.open_app_window(URL, timeout_s=5)
    assert page.url == URL


async def test_open_app_window_last_resort_is_context_new_page(cfg):
    async def spawn(argv):
        pass

    b, ctx, fb = make_browser(cfg, spawn=spawn)
    fb.send_raises["Target.createTarget"] = RuntimeError("denied")
    page = await b.open_app_window(URL, timeout_s=0.05)
    assert ctx.new_pages == 1
    assert page.gotos == [URL]


async def test_open_app_window_raises_when_nothing_works(cfg):
    async def spawn(argv):
        pass

    b, ctx, fb = make_browser(cfg, spawn=spawn)
    fb.send_raises["Target.createTarget"] = RuntimeError("denied")

    async def boom():
        raise RuntimeError("no new page")

    ctx.new_page = boom
    with pytest.raises(RuntimeError, match="comments"):
        await b.open_app_window(URL, timeout_s=0.05)


async def test_spawn_detached_uses_devnull_and_reaps(tmp_path):
    marker = tmp_path / "ran"
    script = tmp_path / "fake-chromium"
    script.write_text(f"#!/bin/sh\necho \"$@\" > {marker}\nexit 0\n")
    script.chmod(0o755)
    reaper = await browser_mod.spawn_detached([str(script), "--user-data-dir=/p", "--app=http://x/"])
    assert await asyncio.wait_for(reaper, 5) == 0  # the exit code
    for _ in range(200):
        if marker.exists() and not browser_mod._REAPERS:
            break
        await asyncio.sleep(0.02)
    assert marker.read_text().strip() == "--user-data-dir=/p --app=http://x/"
    assert not browser_mod._REAPERS  # reaped in the background: no zombie


async def test_default_spawn_is_spawn_detached(cfg):
    b = CdpBrowser(cfg.cdp)
    assert b._spawn is browser_mod.spawn_detached


# ------------------------------------------------------ close_browser


async def test_close_browser_sends_browser_close_and_stops_playwright(cfg):
    b, _, fb = make_browser(cfg)
    pw = b._pw
    await b.close_browser()
    assert ("Browser.close", {}) in fb.calls
    assert pw.stopped
    assert not b.is_connected


async def test_close_browser_swallows_errors(cfg):
    b, _, fb = make_browser(cfg)
    fb.send_raises["Browser.close"] = RuntimeError("Target closed")
    await b.close_browser()  # never raises
    await b.close_browser()  # and is idempotent
    unconnected = CdpBrowser(cfg.cdp)
    await unconnected.close_browser()


async def test_close_browser_is_bounded(cfg, monkeypatch):
    monkeypatch.setattr(browser_mod, "CLOSE_TIMEOUT_S", 0.05)
    b, _, fb = make_browser(cfg)

    async def hang(method, params):
        await asyncio.sleep(30)

    fb.on_send = hang
    await asyncio.wait_for(b.close_browser(), 2)


# ------------------------------------------------------- SSN helper tab


async def test_ssn_helper_tab_opened_in_background_when_extension_installed(cfg):
    write_manifest(cfg.cdp.user_data_dir, "1_0", {"action": {"default_popup": "menu.html"}})
    b, ctx, fb = make_browser(cfg)
    await b._open_ssn_helper()
    want = f"chrome-extension://{SSN_EXTENSION_ID}/menu.html"
    assert ("Target.createTarget", {"url": want, "background": True}) in fb.calls


async def test_ssn_helper_tab_not_duplicated(cfg):
    # Playwright's context.pages omits extension tabs over CDP, so the open helper
    # is only visible as a CDP target.
    write_manifest(cfg.cdp.user_data_dir, "1_0", {"action": {"default_popup": "menu.html"}})
    b, ctx, fb = make_browser(cfg, [FakePage()])
    fb.targets = [
        {"type": "page", "url": "about:blank"},
        {"type": "page", "url": f"chrome-extension://{SSN_EXTENSION_ID}/menu.html"},
    ]
    await b._open_ssn_helper()
    assert "Target.createTarget" not in [m for m, _ in fb.calls]


async def test_ssn_helper_not_confused_by_other_extension_pages(cfg):
    write_manifest(cfg.cdp.user_data_dir, "1_0", {"action": {"default_popup": "menu.html"}})
    b, ctx, fb = make_browser(cfg)
    fb.targets = [
        {"type": "page", "url": f"chrome-extension://{SSN_EXTENSION_ID}/background.html"},
        {"type": "service_worker", "url": f"chrome-extension://{SSN_EXTENSION_ID}/menu.html"},
    ]
    await b._open_ssn_helper()
    want = f"chrome-extension://{SSN_EXTENSION_ID}/menu.html"
    assert ("Target.createTarget", {"url": want, "background": True}) in fb.calls


async def test_ssn_helper_opened_when_targets_cannot_be_listed(cfg):
    # A missing helper loses SSN comments; a duplicate only costs memory.
    write_manifest(cfg.cdp.user_data_dir, "1_0", {"action": {"default_popup": "menu.html"}})
    b, ctx, fb = make_browser(cfg)
    fb.send_raises["Target.getTargets"] = RuntimeError("nope")
    await b._open_ssn_helper()  # no raise
    want = f"chrome-extension://{SSN_EXTENSION_ID}/menu.html"
    assert ("Target.createTarget", {"url": want, "background": True}) in fb.calls


async def test_ssn_helper_absent_extension_does_nothing(cfg):
    b, ctx, fb = make_browser(cfg)
    await b._open_ssn_helper()
    assert fb.calls == [] and ctx.new_pages == 0


async def test_ssn_helper_errors_are_swallowed(cfg):
    write_manifest(cfg.cdp.user_data_dir, "1_0", {"action": {"default_popup": "menu.html"}})
    b, ctx, fb = make_browser(cfg)
    fb.send_raises["Target.createTarget"] = RuntimeError("nope")
    await b._open_ssn_helper()  # no raise


# ------------------------------------------- SSN background page refresh


async def test_refresh_ssn_background_closes_only_ssn_background_tabs(cfg):
    b, ctx, fb = make_browser(cfg)
    fb.targets = [
        {"type": "page", "url": "https://www.facebook.com/live/producer/", "targetId": "FB"},
        {"type": "page", "url": f"chrome-extension://{SSN_EXTENSION_ID}/background.html?x=1", "targetId": "BG"},
        {"type": "page", "url": f"chrome-extension://{SSN_EXTENSION_ID}/popup.html", "targetId": "MENU"},
        {"type": "service_worker", "url": f"chrome-extension://{SSN_EXTENSION_ID}/service_worker.js", "targetId": "SW"},
        {"type": "page", "url": "chrome-extension://otherextension/background.html", "targetId": "OTHER"},
    ]
    assert await b.refresh_ssn_background() == 1
    closes = [c for c in fb.calls if c[0] == "Target.closeTarget"]
    assert closes == [("Target.closeTarget", {"targetId": "BG"})]


async def test_refresh_ssn_background_with_nothing_open_closes_nothing(cfg):
    b, ctx, fb = make_browser(cfg)
    fb.targets = [{"type": "page", "url": "about:blank", "targetId": "A"}]
    assert await b.refresh_ssn_background() == 0
    assert "Target.closeTarget" not in [m for m, _ in fb.calls]


# ----------------------------------------------------- adapter guards


class FakeCdp:
    """Stands in for CdpBrowser in adapter tests."""

    def __init__(self, cfg, *, connected: bool = True):
        self.cfg = cfg
        self.connected = connected
        self.calls: list[tuple] = []
        self.context = object()
        self.pages: list[FakePage] = []
        self.opened: list[str] = []
        self.dashboard = FakePage("about:blank")
        self.reachable_urls: list[str] = []
        self.popout_pages: list = []
        self.connect_raises: Exception | None = None

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.calls.append(("connect",))
        if self.connect_raises is not None:
            raise self.connect_raises
        self.connected = True

    async def dashboard_page(self):
        if not self.connected:
            raise BrowserNotConnected("browser is not connected")
        return self.dashboard

    async def find_page(self, url_prefix=None, *, contains=None):
        for p in self.pages:
            if p.is_closed():
                continue
            if contains is not None and contains in p.url:
                return p
        return None

    async def open_app_window(self, url, timeout_s=20):
        self.opened.append(url)
        page = FakePage(url)
        self.pages.append(page)
        return page

    def track_popout(self, page) -> None:
        if page not in self.popout_pages:
            self.popout_pages.append(page)

    def untrack_popout(self, page) -> None:
        if page in self.popout_pages:
            self.popout_pages.remove(page)

    def open_popouts(self) -> list:
        return [p for p in self.popout_pages if not p.is_closed()]

    async def http_reachable(self, url, timeout_s=5):
        self.reachable_urls.append(url)
        return True

    async def close_browser(self):
        self.calls.append(("close_browser",))

    async def refresh_ssn_background(self):
        self.calls.append(("refresh_ssn_background",))
        return 1


async def test_facebook_adapter_refresh_ssn_connects_then_delegates(cfg):
    cdp = FakeCdp(cfg, connected=False)
    await FacebookAdapter(cfg, cdp).refresh_ssn()
    assert cdp.calls == [("connect",), ("refresh_ssn_background",)]


async def test_facebook_adapter_methods_surface_a_failed_connect_clearly(cfg):
    cdp = FakeCdp(cfg, connected=False)
    cdp.connect_raises = BrowserNotConnected("cannot connect to Chromium")
    fb = FacebookAdapter(cfg, cdp)
    for call in (
        fb.logged_in(),
        fb.setup_live(),
        fb.wait_preview_ready(1),
        fb.preview_lost(),
        fb.go_live(1),
        fb.open_comments(LIVE_ID),
        fb.park_dashboard(),
        fb.save_failure("x", Path("/nonexistent/failure")),
    ):
        with pytest.raises(BrowserNotConnected, match="cannot connect"):
            await call
    await fb.close_browser()  # never raises


async def test_facebook_adapter_reconnects_when_the_browser_is_disconnected(cfg, monkeypatch):
    class P:
        def __init__(self, page, **kw):
            pass

        async def open(self, url):
            pass

        async def is_login_page(self):
            return False

        async def preview_lost(self):
            return False

    monkeypatch.setattr(adapter_mod, "Producer", P)
    for call in ("logged_in", "preview_lost", "open_comments", "park_dashboard"):
        cdp = FakeCdp(cfg, connected=False)
        fb = FacebookAdapter(cfg, cdp)
        args = (LIVE_ID,) if call == "open_comments" else ()
        await getattr(fb, call)(*args)
        assert cdp.calls == [("connect",)], call
        await getattr(fb, call)(*args)
        assert cdp.calls == [("connect",)], call  # connected now: no second connect


async def test_facebook_adapter_does_not_connect_when_already_connected(cfg):
    cdp = FakeCdp(cfg)
    fb = FacebookAdapter(cfg, cdp)
    await fb.open_comments(LIVE_ID)
    await fb.park_dashboard()
    assert cdp.calls == []


async def test_facebook_adapter_reachable_uses_selectors_url_and_no_connection(cfg):
    cdp = FakeCdp(cfg, connected=False)
    fb = FacebookAdapter(cfg, cdp)
    assert await fb.reachable() is True
    assert cdp.reachable_urls == [S.REACHABILITY_URL]


async def test_facebook_adapter_connect_delegates(cfg):
    cdp = FakeCdp(cfg, connected=False)
    await FacebookAdapter(cfg, cdp).connect()
    assert cdp.calls == [("connect",)]


async def test_facebook_adapter_open_comments_uses_template_and_parks_dashboard(cfg):
    cdp = FakeCdp(cfg)
    fb = FacebookAdapter(cfg, cdp)
    await fb.open_comments(LIVE_ID)
    assert cdp.opened == [cfg.facebook.comments_url_template.format(live_id=LIVE_ID)]
    await fb.park_dashboard()
    assert cdp.dashboard.gotos == ["about:blank"]


async def test_facebook_adapter_one_producer_per_dashboard_page(cfg, monkeypatch):
    made = []

    class P:
        def __init__(self, page, **kw):
            made.append((page, kw))

        async def preview_lost(self):
            return False

    monkeypatch.setattr(adapter_mod, "Producer", P)
    cdp = FakeCdp(cfg)
    fb = FacebookAdapter(cfg, cdp, record_dir=Path("/rec"))
    await fb.preview_lost()
    await fb.preview_lost()
    assert len(made) == 1
    page, kw = made[0]
    assert page is cdp.dashboard
    assert kw == {
        "group_name": "Test Group",
        "step_timeout_s": cfg.timeouts.step_s,
        "record_dir": Path("/rec"),
    }
    cdp.dashboard = FakePage()  # the dashboard page changed (browser restarted)
    await fb.preview_lost()
    assert len(made) == 2


async def test_facebook_adapter_logged_in_opens_producer_url(cfg, monkeypatch):
    seen = []

    class P:
        def __init__(self, page, **kw):
            pass

        async def open(self, url):
            seen.append(url)

        async def is_login_page(self):
            return seen[-1] == "login"

    monkeypatch.setattr(adapter_mod, "Producer", P)
    cdp = FakeCdp(cfg)
    fb = FacebookAdapter(cfg, cdp)
    assert await fb.logged_in() is True
    assert seen == [cfg.facebook.producer_url]


async def test_facebook_adapter_save_failure_delegates(cfg, monkeypatch, tmp_path):
    seen = []

    class P:
        def __init__(self, page, **kw):
            pass

        async def save_failure(self, step, directory):
            seen.append((step, directory))

    monkeypatch.setattr(adapter_mod, "Producer", P)
    fb = FacebookAdapter(cfg, FakeCdp(cfg))
    await fb.save_failure("go_live", tmp_path / "d")
    assert seen == [("go_live", tmp_path / "d")]


# ---------------------------------------------------- CaptureAdapter


class FakeDom:
    instances: list["FakeDom"] = []

    def __init__(self, context, **kw):
        self.context = context
        self.attached_page = None
        self.attaches: list[tuple] = []
        self.detaches = 0
        FakeDom.instances.append(self)

    async def attach(self, page, log):
        self.attached_page = page
        self.attaches.append((page, log))

    async def detach(self):
        self.attached_page = None
        self.detaches += 1


class FakeSsn:
    instances: list["FakeSsn"] = []

    def __init__(self, session_id, log_factory, *a, **kw):
        self.session_id = session_id
        self.log_factory = log_factory
        self.cancelled = False
        FakeSsn.instances.append(self)

    async def run(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.fixture
def fakes(monkeypatch):
    FakeDom.instances = []
    FakeSsn.instances = []
    monkeypatch.setattr(adapter_mod, "FbDomCapture", FakeDom)
    monkeypatch.setattr(adapter_mod, "SsnListener", FakeSsn)


def popout_url(live_id=LIVE_ID):
    return f"http://127.0.0.1:1/live/producer/dashboard/{live_id}/COMMENTS/"


async def test_capture_start_attaches_to_the_popout_and_writes_into_stream_dir(cfg, tmp_path, fakes):
    cdp = FakeCdp(cfg)
    popout = FakePage(popout_url())
    cdp.pages.append(popout)
    cap = CaptureAdapter(cfg, cdp)
    assert cap.healthy() is False
    await cap.start(LIVE_ID, tmp_path / "s1")
    dom = FakeDom.instances[0]
    (page, log) = dom.attaches[0]
    assert page is popout
    assert isinstance(log, CommentLog)
    assert log.path == tmp_path / "s1" / "comments.jsonl" and log.live_id == LIVE_ID
    assert cdp.opened == []
    assert cap.healthy() is True
    await popout.close()
    assert cap.healthy() is False
    await cap.stop()


async def test_capture_start_opens_the_popout_itself_when_missing(cfg, tmp_path, fakes):
    cdp = FakeCdp(cfg)
    cap = CaptureAdapter(cfg, cdp)
    await cap.start(LIVE_ID, tmp_path)
    assert cdp.opened == [cfg.facebook.comments_url_template.format(live_id=LIVE_ID)]
    assert FakeDom.instances[0].attaches[0][0].url == cdp.opened[0]
    await cap.stop()


async def test_capture_start_connects_when_browser_is_not_connected(cfg, tmp_path, fakes):
    cdp = FakeCdp(cfg, connected=False)
    cdp.pages.append(FakePage(popout_url()))
    cap = CaptureAdapter(cfg, cdp)
    await cap.start(LIVE_ID, tmp_path)
    assert cdp.calls == [("connect",)]
    await cap.stop()


async def test_capture_one_dom_capture_per_context_recreated_when_context_changes(cfg, tmp_path, fakes):
    cdp = FakeCdp(cfg)
    cdp.pages.append(FakePage(popout_url()))
    cap = CaptureAdapter(cfg, cdp)
    await cap.start(LIVE_ID, tmp_path)
    await cap.start(LIVE_ID, tmp_path)
    assert len(FakeDom.instances) == 1
    cdp.context = object()  # browser reconnected: new context object
    await cap.start(LIVE_ID, tmp_path)
    assert len(FakeDom.instances) == 2
    assert FakeDom.instances[1].context is cdp.context
    await cap.stop()


async def test_capture_restart_same_live_reuses_the_comment_log(cfg, tmp_path, fakes):
    cdp = FakeCdp(cfg)
    cdp.pages.append(FakePage(popout_url()))
    cap = CaptureAdapter(cfg, cdp)
    await cap.start(LIVE_ID, tmp_path / "a")
    await cap.start(LIVE_ID, tmp_path / "a")
    logs = [log for _, log in FakeDom.instances[0].attaches]
    assert logs[0] is logs[1]
    await cap.start(LIVE_ID, tmp_path / "b")  # different dir: a new log
    assert FakeDom.instances[0].attaches[2][1] is not logs[0]
    await cap.stop()


async def test_capture_stop_detaches_keeps_file_and_tolerates_no_start(cfg, tmp_path, fakes):
    cdp = FakeCdp(cfg)
    cap = CaptureAdapter(cfg, cdp)
    await cap.stop()  # no completed start: no error
    cdp.pages.append(FakePage(popout_url()))
    await cap.start(LIVE_ID, tmp_path)
    log = FakeDom.instances[0].attaches[0][1]
    log.path.write_text('{"x": 1}\n')
    await cap.stop()
    assert FakeDom.instances[0].detaches == 1
    assert log.path.read_text() == '{"x": 1}\n'
    assert cap.healthy() is False
    await cap.stop()


async def test_capture_stop_after_failed_start_does_not_raise(cfg, tmp_path, fakes, monkeypatch):
    class Boom(FakeDom):
        async def attach(self, page, log):
            raise RuntimeError("attach failed")

    monkeypatch.setattr(adapter_mod, "FbDomCapture", Boom)
    cdp = FakeCdp(cfg)
    cdp.pages.append(FakePage(popout_url()))
    cap = CaptureAdapter(cfg, cdp)
    with pytest.raises(RuntimeError, match="attach failed"):
        await cap.start(LIVE_ID, tmp_path)
    assert cap.healthy() is False
    await cap.stop()


async def test_capture_ssn_listener_started_once_and_cancelled_on_stop(cfg, tmp_path, fakes):
    import dataclasses as dc

    cfg2 = dc.replace(cfg, ssn=dc.replace(cfg.ssn, enabled=True, session_id="sess-123"))
    cdp = FakeCdp(cfg2)
    cdp.pages.append(FakePage(popout_url()))
    cap = CaptureAdapter(cfg2, cdp)
    await cap.start(LIVE_ID, tmp_path)
    await cap.start(LIVE_ID, tmp_path)  # re-attach: no second listener
    await asyncio.sleep(0)
    assert len(FakeSsn.instances) == 1
    ssn = FakeSsn.instances[0]
    assert ssn.session_id == "sess-123"
    log = FakeDom.instances[0].attaches[0][1]
    assert ssn.log_factory() is log
    await cap.stop()
    assert ssn.cancelled
    assert ssn.log_factory() is None


@pytest.mark.parametrize("enabled, session", [(False, "sess"), (True, "")])
async def test_capture_ssn_not_started_when_disabled_or_no_session(cfg, tmp_path, fakes, enabled, session):
    import dataclasses as dc

    cfg2 = dc.replace(cfg, ssn=dc.replace(cfg.ssn, enabled=enabled, session_id=session))
    cdp = FakeCdp(cfg2)
    cdp.pages.append(FakePage(popout_url()))
    cap = CaptureAdapter(cfg2, cdp)
    await cap.start(LIVE_ID, tmp_path)
    assert FakeSsn.instances == []
    await cap.stop()


async def test_capture_ssn_task_restarted_if_it_died(cfg, tmp_path, fakes, monkeypatch):
    import dataclasses as dc

    class Dies(FakeSsn):
        async def run(self):
            raise RuntimeError("websockets missing")

    monkeypatch.setattr(adapter_mod, "SsnListener", Dies)
    cfg2 = dc.replace(cfg, ssn=dc.replace(cfg.ssn, enabled=True, session_id="sess"))
    cdp = FakeCdp(cfg2)
    cdp.pages.append(FakePage(popout_url()))
    cap = CaptureAdapter(cfg2, cdp)
    await cap.start(LIVE_ID, tmp_path)
    await asyncio.sleep(0.01)
    await cap.start(LIVE_ID, tmp_path)
    assert len(FakeSsn.instances) == 2
    await cap.stop()


# ------------------------------------- bounded connect cleanup / close (review I1, m7)


class _FakePw:
    def __init__(self, *, connect_exc=None, connect_hangs=False, stop_hangs=False):
        self.connect_exc, self.connect_hangs, self.stop_hangs = connect_exc, connect_hangs, stop_hangs
        self.stop_called = False
        outer = self

        class Chromium:
            async def connect_over_cdp(self, url):
                if outer.connect_hangs:
                    await asyncio.sleep(30)
                raise outer.connect_exc

        self.chromium = Chromium()

    async def stop(self):
        self.stop_called = True
        if self.stop_hangs:
            await asyncio.sleep(30)


@pytest.fixture
def fake_playwright(monkeypatch):
    """Install a fake ``playwright.async_api`` so ``CdpBrowser.connect`` runs on the host."""
    import sys
    import types

    holder = {}

    class _Starter:
        async def start(self):
            return holder["pw"]

    mod = types.ModuleType("playwright.async_api")
    mod.async_playwright = lambda: _Starter()
    pkg = types.ModuleType("playwright")
    pkg.async_api = mod
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", mod)
    monkeypatch.setattr(browser_mod, "CLOSE_TIMEOUT_S", 0.05)
    return holder


async def test_connect_failure_cleanup_is_bounded_when_playwright_stop_hangs(cfg, fake_playwright):
    pw = fake_playwright["pw"] = _FakePw(connect_exc=RuntimeError("refused"), stop_hangs=True)
    b = CdpBrowser(cfg.cdp)
    with pytest.raises(RuntimeError, match="refused"):
        await asyncio.wait_for(b.connect(), 2)
    assert pw.stop_called
    assert not b.is_connected


async def test_cancelled_connect_does_not_hang_on_a_stuck_playwright_stop(cfg, fake_playwright):
    pw = fake_playwright["pw"] = _FakePw(connect_hangs=True, stop_hangs=True)
    b = CdpBrowser(cfg.cdp)
    task = asyncio.ensure_future(b.connect())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if task is not None and not task.done():
            break
    await asyncio.sleep(0.05)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=2)
    assert done, "connect() did not unwind after a cancel"
    assert task.cancelled()
    assert pw.stop_called


async def test_close_browser_propagates_cancellation(cfg):
    b, _, fb = make_browser(cfg)

    async def hang(method, params):
        await asyncio.sleep(30)

    fb.on_send = hang
    task = asyncio.ensure_future(b.close_browser())
    await asyncio.sleep(0.05)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=2)
    assert done and task.cancelled()


# ------------------------------------- no window pile-up (review I3)

REDIRECTED = "http://x.test/login/checkpoint?next=comments"


async def test_open_app_window_adopts_a_redirected_handoff_window(cfg, caplog):
    holder = {}

    async def spawn(argv):
        async def later():
            await asyncio.sleep(0.02)
            holder["ctx"].pages.append(FakePage(REDIRECTED))

        asyncio.ensure_future(later())

    dashboard = FakePage("about:blank")
    b, ctx, fb = make_browser(cfg, [dashboard], spawn=spawn)
    holder["ctx"] = ctx
    before = len(ctx.pages)
    with caplog.at_level(logging.WARNING):
        page = await b.open_app_window(URL, timeout_s=0.15)
    assert page.url == REDIRECTED and page is not dashboard
    assert len(ctx.pages) - before == 1  # one window for this attempt
    assert fb.calls == [] and ctx.new_pages == 0  # no CDP window, no plain page
    assert page in b.popout_pages


async def test_open_app_window_adopts_a_createtarget_page_with_unexpected_url(cfg):
    async def spawn(argv):
        pass

    b, ctx, fb = make_browser(cfg, [FakePage("about:blank")], spawn=spawn)

    async def on_send(method, params):
        if method == "Target.createTarget":
            ctx.pages.append(FakePage(REDIRECTED))

    fb.on_send = on_send
    b.fallback_wait_s = 0.1
    before = len(ctx.pages)
    page = await b.open_app_window(URL, timeout_s=0.05)
    assert page.url == REDIRECTED
    assert len(ctx.pages) - before == 1
    assert ctx.new_pages == 0


async def test_open_app_window_never_adopts_existing_extension_or_tracked_pages(cfg):
    ext = FakePage(f"chrome-extension://{SSN_EXTENSION_ID}/popup.html")

    async def spawn(argv):
        async def later():
            await asyncio.sleep(0.01)
            ctx.pages.append(FakePage("devtools://devtools/x"))
            ctx.pages.append(FakePage(f"chrome-extension://{SSN_EXTENSION_ID}/other.html"))

        asyncio.ensure_future(later())

    b, ctx, fb = make_browser(cfg, [FakePage("about:blank"), ext], spawn=spawn)
    b.fallback_wait_s = 0.05
    page = await b.open_app_window(URL, timeout_s=0.1)
    assert ctx.new_pages == 1  # nothing adoptable: the last resort plain page
    assert page.gotos == [URL]


async def test_open_popouts_and_untrack(cfg):
    p1, p2 = FakePage(URL), FakePage(URL)
    b, _, _ = make_browser(cfg, [p1, p2])
    b.track_popout(p1)
    b.track_popout(p2)
    assert b.open_popouts() == [p1, p2]
    await p1.close()
    assert b.open_popouts() == [p2]
    b.untrack_popout(p2)
    assert b.open_popouts() == []
    b.untrack_popout(p2)  # idempotent


# ------------------------------- capture adapter popout handling (review m5, m8, I3)


def test_popout_url_fragment_lives_in_selectors():
    frag = S.popout_url_fragment(LIVE_ID)
    assert frag == f"/{LIVE_ID}{S.COMMENTS_PATH_MARKER}"
    assert S.is_popout_url(f"http://h/live/producer/dashboard/{LIVE_ID}{S.COMMENTS_PATH_MARKER}/")
    assert not S.is_popout_url("http://h/login")


async def test_capture_reuses_a_tracked_redirected_popout_instead_of_opening_another(cfg, tmp_path, fakes):
    cdp = FakeCdp(cfg)
    stray = FakePage("http://127.0.0.1:1/login/checkpoint")
    cdp.pages.append(stray)
    cdp.track_popout(stray)
    cap = CaptureAdapter(cfg, cdp)
    await cap.start(LIVE_ID, tmp_path)
    assert cdp.opened == []
    assert FakeDom.instances[0].attaches[0][0] is stray
    assert stray.gotos == [cfg.facebook.comments_url_template.format(live_id=LIVE_ID)]
    await cap.stop()


async def test_capture_does_not_reuse_a_stale_popout_of_another_live(cfg, tmp_path, fakes):
    cdp = FakeCdp(cfg)
    stale = FakePage(popout_url("9999999999"))
    cdp.pages.append(stale)
    cdp.track_popout(stale)
    cap = CaptureAdapter(cfg, cdp)
    await cap.start(LIVE_ID, tmp_path)
    assert len(cdp.opened) == 1
    assert FakeDom.instances[0].attaches[0][0] is not stale
    await cap.stop()


async def test_capture_stop_closes_and_untracks_the_popouts(cfg, tmp_path, fakes):
    cdp = FakeCdp(cfg)
    popout = FakePage(popout_url())
    dashboard = FakePage("about:blank")
    cdp.pages.extend([popout, dashboard])
    cap = CaptureAdapter(cfg, cdp)
    await cap.start(LIVE_ID, tmp_path)
    assert cdp.popout_pages == [popout]
    await cap.stop()
    assert popout.is_closed() and popout.closes == 1
    assert not dashboard.is_closed()
    assert cdp.popout_pages == []
    await cap.stop()  # second stop: nothing left to close
    assert popout.closes == 1


async def test_capture_stop_popout_close_is_bounded_and_never_raises(cfg, tmp_path, fakes, monkeypatch):
    monkeypatch.setattr(adapter_mod, "_CLOSE_POPOUT_S", 0.05)
    cdp = FakeCdp(cfg)
    popout = FakePage(popout_url())
    popout.close_hangs = True
    cdp.pages.append(popout)
    cap = CaptureAdapter(cfg, cdp)
    await cap.start(LIVE_ID, tmp_path)
    await asyncio.wait_for(cap.stop(), 2)
    # not closed in time: stays tracked (so it is never mistaken for the dashboard)
    assert cdp.popout_pages == [popout]

    class Boom(FakePage):
        async def close(self):
            raise RuntimeError("Target closed")

    boom = Boom(popout_url("5"))
    cdp.pages.append(boom)
    cdp.track_popout(boom)
    await cap.stop()  # swallowed
    assert boom in cdp.popout_pages and popout in cdp.popout_pages


async def test_capture_stop_untracks_a_popout_only_after_it_closed(cfg, tmp_path, fakes, monkeypatch):
    monkeypatch.setattr(adapter_mod, "_CLOSE_POPOUT_S", 0.05)
    cdp = FakeCdp(cfg)
    popout = FakePage(popout_url())
    cdp.pages.append(popout)
    cap = CaptureAdapter(cfg, cdp)
    await cap.start(LIVE_ID, tmp_path)
    popout.close_hangs = True
    await cap.stop()
    assert cdp.popout_pages == [popout] and not popout.is_closed()
    popout.close_hangs = False
    await cap.stop()  # retried: now it closes and is untracked
    assert popout.is_closed() and cdp.popout_pages == []


# ------------------------------------------- stop-generation guard (re-review N1)


def _ssn_cfg(cfg):
    return dataclasses.replace(cfg, ssn=dataclasses.replace(cfg.ssn, enabled=True, session_id="sess"))


async def test_start_released_after_stop_while_connecting_leaves_nothing(cfg, tmp_path, fakes):
    cfg2 = _ssn_cfg(cfg)
    cdp = FakeCdp(cfg2, connected=False)
    gate = asyncio.Event()
    real_connect = cdp.connect

    async def slow_connect():
        await gate.wait()
        await real_connect()

    cdp.connect = slow_connect
    cdp.pages.append(FakePage(popout_url()))
    cap = CaptureAdapter(cfg2, cdp)
    task = asyncio.ensure_future(cap.start(LIVE_ID, tmp_path))
    await asyncio.sleep(0.02)
    await cap.stop()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cap._log is None and cap._ssn_task is None
    assert FakeSsn.instances == [] and not (tmp_path / "comments.jsonl").exists()
    assert cap.healthy() is False


async def test_start_released_after_stop_while_opening_the_popout_closes_it(cfg, tmp_path, fakes):
    cfg2 = _ssn_cfg(cfg)
    cdp = FakeCdp(cfg2)
    gate = asyncio.Event()
    real_open = cdp.open_app_window

    async def slow_open(url, timeout_s=20):
        await gate.wait()
        page = await real_open(url, timeout_s)
        cdp.track_popout(page)
        return page

    cdp.open_app_window = slow_open
    cap = CaptureAdapter(cfg2, cdp)
    task = asyncio.ensure_future(cap.start(LIVE_ID, tmp_path))
    await asyncio.sleep(1.2)  # past the popout grace: it is opening one
    assert len(FakeSsn.instances) == 1  # the SSN task was started before the popout wait
    await cap.stop()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cap._log is None and cap._ssn_task is None
    assert FakeSsn.instances[0].cancelled
    opened = cdp.pages[-1]
    assert opened.is_closed() and cdp.popout_pages == []
    assert FakeDom.instances[0].attaches == []


async def test_start_released_after_stop_while_attaching_detaches(cfg, tmp_path, fakes, monkeypatch):
    gate = asyncio.Event()

    class SlowDom(FakeDom):
        async def attach(self, page, log):
            await gate.wait()
            await super().attach(page, log)

    monkeypatch.setattr(adapter_mod, "FbDomCapture", SlowDom)
    cdp = FakeCdp(cfg)
    cdp.pages.append(FakePage(popout_url()))
    cap = CaptureAdapter(cfg, cdp)
    task = asyncio.ensure_future(cap.start(LIVE_ID, tmp_path))
    await asyncio.sleep(0.02)
    await cap.stop()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cap.healthy() is False
    assert FakeDom.instances[0].attached_page is None
    assert cap._log is None


async def test_a_normal_start_after_a_stop_still_works(cfg, tmp_path, fakes):
    cdp = FakeCdp(cfg)
    cdp.pages.append(FakePage(popout_url()))
    cap = CaptureAdapter(cfg, cdp)
    await cap.start(LIVE_ID, tmp_path / "a")
    await cap.stop()
    cdp.pages.append(FakePage(popout_url()))
    await cap.start(LIVE_ID, tmp_path / "b")
    assert cap.healthy() is True
    await cap.stop()


# ------------------------------------------- adoption never takes the SSN helper (N4)


async def test_open_app_window_never_adopts_a_chrome_error_page(cfg):
    async def spawn(argv):
        async def later():
            await asyncio.sleep(0.01)
            ctx.pages.append(FakePage("chrome-error://chromewebdata/"))  # the helper failed to load

        asyncio.ensure_future(later())

    b, ctx, fb = make_browser(cfg, [FakePage("about:blank")], spawn=spawn)
    b.fallback_wait_s = 0.05
    page = await b.open_app_window(URL, timeout_s=0.1)
    assert page.url != "chrome-error://chromewebdata/"
    assert ctx.new_pages == 1


async def test_open_app_window_never_adopts_the_remembered_ssn_helper_page(cfg):
    write_manifest(cfg.cdp.user_data_dir, "1_0", {"action": {"default_popup": "menu.html"}})
    helper_url = f"chrome-extension://{SSN_EXTENSION_ID}/menu.html"

    async def spawn(argv):
        pass

    b, ctx, fb = make_browser(cfg, [FakePage("about:blank")], spawn=spawn)

    async def on_send(method, params):
        if params.get("background"):
            ctx.pages.append(FakePage("about:blank"))  # helper tab, URL not yet loaded

    fb.on_send = on_send
    await b._open_ssn_helper()
    helper = ctx.pages[-1]
    assert b._helper_pages == [helper]
    assert helper_url  # (the helper page was created for this URL)
    b.fallback_wait_s = 0.05
    page = await b.open_app_window(URL, timeout_s=0.05)
    assert page is not helper


# ------------------------------------------- shielded reconnect, leak-free start (N5)


async def test_adapter_reconnect_survives_a_cancel_of_the_caller(cfg, monkeypatch):
    class P:
        def __init__(self, page, **kw):
            pass

        async def preview_lost(self):
            return False

    monkeypatch.setattr(adapter_mod, "Producer", P)
    cdp = FakeCdp(cfg, connected=False)

    async def slow_connect():
        cdp.calls.append(("connect",))
        await asyncio.sleep(0.1)
        cdp.connected = True

    cdp.connect = slow_connect
    fb = FacebookAdapter(cfg, cdp)
    task = asyncio.ensure_future(fb.preview_lost())
    await asyncio.sleep(0.02)
    task.cancel()  # e.g. the controller's 5 s preview_lost bound
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert cdp.connected is True  # the connect still finished in the background
    assert cdp.calls == [("connect",)]
    assert await fb.preview_lost() is False  # next call: already connected
    assert cdp.calls == [("connect",)]


async def test_cancel_during_playwright_start_does_not_leak_the_driver(cfg, monkeypatch):
    import sys
    import types

    pw = _FakePw()

    class _Starter:
        async def start(self):
            await asyncio.sleep(0.05)
            return pw

    mod = types.ModuleType("playwright.async_api")
    mod.async_playwright = lambda: _Starter()
    pkg = types.ModuleType("playwright")
    pkg.async_api = mod
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", mod)
    b = CdpBrowser(cfg.cdp)
    task = asyncio.ensure_future(b.connect())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert pw.stop_called  # the late-started driver was stopped
    assert not b.is_connected
