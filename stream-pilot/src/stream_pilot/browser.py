"""CdpBrowser: one Playwright connection to the appliance's running Chromium.

Chromium is started by its own systemd unit with a remote-debugging port;
stream-pilot attaches with ``connect_over_cdp`` and drives the first browser
context (the real profile, with the Facebook login and the SSN extension).

Playwright is imported lazily inside :meth:`CdpBrowser.connect`, so this module
(and everything that imports it) loads on a host without Playwright. Nothing
here knows Facebook's markup or URLs: callers pass URLs in. The Social Stream
Ninja (SSN) extension id and its helper page name are SSN knowledge and live
here (constants below).

The SSN helper tab: the extension's MV3 helper page must stay open in the main
browser window for the extension to relay comments. After :meth:`connect` the
page named by the extension's ``manifest.json`` (looked up in the profile's
``Extensions/`` dir at runtime) is opened in a background tab; if the
extension is not installed nothing happens.

Never block the event loop: stdlib HTTP runs in a thread and the Chromium
handoff process is reaped by a background task.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import posixpath
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlsplit

from stream_pilot.config import CdpCfg

if TYPE_CHECKING:  # Playwright is only installed in the image
    from playwright.async_api import Browser, BrowserContext, Page

log = logging.getLogger(__name__)

# Social Stream Ninja Chrome extension (id and helper page are SSN knowledge, not Facebook's).
SSN_EXTENSION_ID = "cppibjhfemifednoimlblfcmjgfhfjeg"
SSN_FALLBACK_PAGE = "popup.html"
# The pinned "Keep Open" tab the extension opens (and reopens) itself.
SSN_BACKGROUND_PAGE = "background.html"
# Summary of the desktop notification SSN posts when that tab is stuck.
SSN_STALLED_SUMMARY = "Social Stream background stopped"

CLOSE_TIMEOUT_S = 10.0  # bound for Browser.close + stopping Playwright
CONNECT_TIMEOUT_S = 30.0
PROBE_TIMEOUT_S = 5.0  # bound for each step of the alive() check
_APP_FALLBACK_WAIT_S = 10.0  # how long a CDP-created window gets to show up
_POLL_S = 0.2
_IGNORED_SCHEMES = ("chrome-extension://", "devtools://")
# Never adopt these as "the window we just opened": a failed-to-load extension page is chrome-error://.
_NOT_ADOPTABLE_SCHEMES = _IGNORED_SCHEMES + ("chrome-error://",)

# What stream-pilot's own tab shows while it is not needed (during a live).
PARK_URL = "data:text/html;charset=utf-8,"
PARK_HTML = (
    "<title>stream-pilot</title><body style='font:22px sans-serif;margin:3em;color:#444'>"
    "<p>stream-pilot uses this tab for Facebook Live.</p>"
    "<p>For anything else, open a new tab (+) or a new window.</p></body>"
)
# Marks the tabs read_in_background opens (one number per tab).
_READ_MARKER = "stream-pilot-read"
_READ_IDS = itertools.count(1)
_HEAVY_RESOURCE_TYPES = frozenset({"media", "image", "font"})
_VIDEO_CDN_RE = re.compile(r"^https?://video[^/]*\.fbcdn\.net/", re.I)


async def _abort_heavy(route: Any) -> None:
    req = route.request
    if req.resource_type in _HEAVY_RESOURCE_TYPES or _VIDEO_CDN_RE.match(req.url):
        await route.abort()
    else:
        await route.continue_()


# Background reapers for spawned handoff processes (never leave a zombie).
_REAPERS: set[asyncio.Task] = set()


class BrowserNotConnected(RuntimeError):
    """The browser adapter was used before ``connect()`` (or after the browser went away)."""


class BrowserError(RuntimeError):
    """A browser operation failed in every way we know to try."""


# ---------------------------------------------------------------- SSN helper


def _version_key(name: str) -> tuple:
    """Sort key for Chromium extension version dirs such as ``1.10_0`` (numeric, newest last)."""
    parts = re.split(r"[._-]", name)
    return tuple((0, int(p)) if p.isdigit() else (-1, 0) for p in parts)


def _declared_popup(manifest: Any) -> str | None:
    if not isinstance(manifest, dict):
        return None
    for key in ("action", "browser_action", "page_action"):
        section = manifest.get(key)
        if isinstance(section, dict):
            popup = section.get("default_popup")
            if isinstance(popup, str) and popup.strip():
                return popup.strip()
    return None


def _safe_extension_path(page: str) -> str | None:
    """``page`` (maybe with ``./`` and a query) as a path inside the extension, else None."""
    path, sep, tail = page.partition("?")
    path, hsep, frag = path.partition("#")
    norm = posixpath.normpath(path.lstrip("/"))
    if norm in ("", ".") or norm == ".." or norm.startswith("../"):
        return None
    return norm + (sep + tail if sep else "") + (hsep + frag if hsep else "")


def ssn_helper_url(user_data_dir: Path) -> str | None:
    """URL of the SSN extension's helper page, or None when it cannot be determined.

    Reads ``<profile>/Default/Extensions/<id>/<newest version>/manifest.json``:
    the page is ``action.default_popup`` (or the MV2 equivalents); only if the
    manifest declares none is ``popup.html`` used, and only if that file exists.
    """
    root = Path(user_data_dir) / "Default" / "Extensions" / SSN_EXTENSION_ID
    try:
        versions = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: _version_key(d.name))
    except OSError:
        return None
    for vdir in reversed(versions):  # newest first; fall through to older ones if broken
        try:
            manifest = json.loads((vdir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        page = _declared_popup(manifest)
        if page is None and (vdir / SSN_FALLBACK_PAGE).is_file():
            page = SSN_FALLBACK_PAGE
        safe = _safe_extension_path(page) if page else None
        if safe:
            return f"chrome-extension://{SSN_EXTENSION_ID}/{safe}"
        return None
    return None


# --------------------------------------------------------------------- spawn


async def spawn_detached(
    argv: list[str], *, env: dict[str, str] | None = None
) -> "asyncio.Task[int]":
    """Start ``argv`` with all stdio on /dev/null and reap it in the background.

    The Chromium ``--app=`` invocation hands its command line to the running
    instance and exits immediately; reaping avoids a zombie either way. Returns
    the reaper task (its result is the exit code) so callers can notice a
    handoff process that failed instead of waiting out a timeout.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    task = asyncio.ensure_future(proc.wait())
    _REAPERS.add(task)
    task.add_done_callback(_REAPERS.discard)
    return task


def _origin_path_prefix(url: str) -> str:
    """``scheme://host/path`` of ``url`` (no query or fragment)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


# ------------------------------------------------------------------- browser


class CdpBrowser:
    """The shared CDP connection.

    ``spawn`` (default :func:`spawn_detached`) starts the Chromium ``--app=``
    handoff process; tests inject their own.
    """

    def __init__(
        self,
        cfg: CdpCfg,
        *,
        spawn: Callable[[list[str]], Awaitable[Any]] | None = None,
    ) -> None:
        self.cfg = cfg
        self._spawn = spawn if spawn is not None else spawn_detached
        self.poll_s = _POLL_S
        self.fallback_wait_s = _APP_FALLBACK_WAIT_S
        self.helper_wait_s = 2.0  # how long to look for the SSN helper page after creating it
        self.ssn_reopen_wait_s = 15.0  # how long SSN gets to reopen its background page itself
        self._helper_pages: list["Page"] = []
        self.popout_pages: list["Page"] = []
        self._dashboard: "Page | None" = None
        self._may_adopt = True
        self._pw: Any = None
        self._browser: "Browser | None" = None
        self._context: "BrowserContext | None" = None
        self._connect_lock = asyncio.Lock()

    # ---- connection ---------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        b = self._browser
        if b is None or self._context is None:
            return False
        try:
            return bool(b.is_connected())
        except Exception:
            return False

    async def alive(self) -> bool:
        """Connected and answering: Playwright's ``is_connected()`` stays True after
        its node driver died, while every call fails with TargetClosedError."""
        if not self.is_connected:
            return False
        try:
            session = await asyncio.wait_for(self._browser.new_browser_cdp_session(), PROBE_TIMEOUT_S)
            await asyncio.wait_for(session.detach(), PROBE_TIMEOUT_S)
            return True
        except Exception as exc:  # CancelledError propagates
            log.warning("CDP connection is not answering (%r); reconnecting", exc)
            return False

    @property
    def context(self) -> "BrowserContext":
        if not self.is_connected or self._context is None:
            raise BrowserNotConnected("browser is not connected (connect() first)")
        return self._context

    def _bind(self, pw: Any, browser: Any, context: Any) -> None:
        self._pw, self._browser, self._context = pw, browser, context

    async def connect(self) -> None:
        """Attach to Chromium over CDP (no-op when already connected; reconnects after a drop)."""
        async with self._connect_lock:
            if await self.alive():
                return
            await self._stop_playwright()
            from playwright.async_api import async_playwright  # lazy: only in the image

            # The driver start runs as its own task: a cancel while it is starting must
            # not leak the node process (it is stopped as soon as it finished starting).
            start_task = asyncio.ensure_future(async_playwright().start())
            pw: Any = None
            try:
                pw = await asyncio.shield(start_task)
                browser = await asyncio.wait_for(
                    pw.chromium.connect_over_cdp(self.cfg.url), CONNECT_TIMEOUT_S
                )
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
            except BaseException:
                # Bounded and shielded: a hung driver must not make a cancelled or
                # failed connect (e.g. a capture re-attach) hang its caller.
                if pw is None:
                    self._stop_late_driver(start_task)
                else:
                    try:
                        await asyncio.wait_for(asyncio.shield(pw.stop()), CLOSE_TIMEOUT_S)
                    except BaseException:
                        pass
                raise
            self._bind(pw, browser, context)
            self.popout_pages = []
            self._helper_pages = []
            self._dashboard = None
            self._may_adopt = True
            log.info("connected to Chromium at %s", self.cfg.url)
        await self._restore_downloads()
        await self._open_ssn_helper()

    async def _restore_downloads(self) -> None:
        """Give downloads back to Chromium.

        Playwright's CDP attach switches the browser to "allowAndName" into a temp
        directory of its own: files got random names, "Show in folder" and opening
        them failed (2026-11-26). With "default" Chromium saves to ~/Downloads under
        the real name, as without stream-pilot.
        """
        try:
            session = await self._browser_session()
            await session.send("Browser.setDownloadBehavior", {"behavior": "default"})
        except Exception as exc:
            log.warning("cannot restore Chromium's own download handling: %r", exc)

    @staticmethod
    def _stop_late_driver(start_task: "asyncio.Future") -> None:
        """Stop a Playwright driver whose start was interrupted, once it has started."""

        def finish(t: "asyncio.Future") -> None:
            if t.cancelled() or t.exception() is not None:
                return
            stopper = asyncio.ensure_future(
                asyncio.wait_for(t.result().stop(), CLOSE_TIMEOUT_S)
            )
            stopper.add_done_callback(lambda f: f.cancelled() or f.exception())

        start_task.add_done_callback(finish)

    async def _stop_playwright(self) -> None:
        pw, self._pw = self._pw, None
        self._browser = self._context = None
        if pw is not None:
            try:
                await asyncio.wait_for(pw.stop(), CLOSE_TIMEOUT_S)
            except Exception as exc:
                log.debug("stopping playwright failed: %r", exc)

    async def disconnect(self) -> None:
        """Drop the CDP connection and stop Playwright's driver; Chromium keeps running. Never raises."""
        await self._stop_playwright()
        self.popout_pages = []

    async def close_browser(self) -> None:
        """Ask Chromium to quit (``Browser.close``), then drop Playwright. Never raises."""
        browser = self._browser
        if browser is not None:
            try:
                await asyncio.wait_for(self._browser_close(browser), CLOSE_TIMEOUT_S)
            except Exception as exc:  # CancelledError / KeyboardInterrupt propagate
                log.debug("Browser.close failed (already closed?): %r", exc)
        try:
            await self._stop_playwright()
        except Exception as exc:  # pragma: no cover - _stop_playwright already swallows
            log.debug("stop failed: %r", exc)
        self.popout_pages = []

    @staticmethod
    async def _browser_close(browser: Any) -> None:
        session = await browser.new_browser_cdp_session()
        await session.send("Browser.close", {})

    async def _browser_session(self) -> Any:
        if not self.is_connected or self._browser is None:
            raise BrowserNotConnected("browser is not connected (connect() first)")
        return await self._browser.new_browser_cdp_session()

    # ---- pages --------------------------------------------------------------

    def track_popout(self, page: "Page") -> None:
        """Remember ``page`` as the comments popout (never returned by :meth:`dashboard_page`)."""
        self.popout_pages = [p for p in self.popout_pages if not p.is_closed()]
        if page not in self.popout_pages:
            self.popout_pages.append(page)

    def untrack_popout(self, page: "Page") -> None:
        if page in self.popout_pages:
            self.popout_pages.remove(page)

    def open_popouts(self) -> list["Page"]:
        """Tracked popout pages that are still open."""
        return [p for p in self.popout_pages if not p.is_closed()]

    def _open_pages(self) -> list["Page"]:
        return [p for p in self.context.pages if not p.is_closed()]

    async def dashboard_page(self, adopt: tuple[str, ...] = ()) -> "Page":
        """stream-pilot's own tab (the producer page, parked while live).

        Remembered while connected. Right after a connect it may adopt an open tab
        that is clearly its own (Chromium's start page ``about:blank``, the parked
        page, or a URL starting with one of ``adopt``); otherwise, and whenever its
        tab was closed, a new tab is opened in the background. The host's tabs are
        never taken.
        """
        page = self._dashboard
        if page is not None and not page.is_closed() and page in self._open_pages():
            return page
        self._dashboard = None
        if self._may_adopt:
            self._may_adopt = False
            for candidate in self._open_pages():
                url = candidate.url or ""
                if (candidate in self.popout_pages or candidate in self._helper_pages
                        or url.startswith(_IGNORED_SCHEMES)):
                    continue
                if url in ("", "about:blank") or url.startswith(PARK_URL) or any(
                    url.startswith(a) for a in adopt if a
                ):
                    self._dashboard = candidate
                    return candidate
        self._dashboard = await self._new_background_page()
        return self._dashboard

    async def _new_background_page(self) -> "Page":
        """A new tab that does not come to the front (the host may be using another one)."""
        marker = f"#{_READ_MARKER}-{next(_READ_IDS)}"
        try:
            session = await self._browser_session()
            await session.send("Target.createTarget", {"url": "about:blank" + marker, "background": True})
            page = await self._wait_for_page("about:blank", self.fallback_wait_s, contains=marker)
            if page is not None:
                return page
            log.warning("the background tab did not show up; opening a plain one")
        except BrowserNotConnected:
            raise
        except Exception as exc:
            log.warning("Target.createTarget failed (%r); opening a plain tab", exc)
        return await self.context.new_page()

    async def read_in_background(
        self, url: str, script: str, *, ready_selector: str, timeout_s: float, settle_s: float = 1.5
    ) -> Any:
        """Load ``url`` in a new background tab, return ``script`` evaluated there, close the tab.

        Waits for ``ready_selector``, then until the number of matches stayed the
        same for ``settle_s`` (pages render lists in chunks), at most ``timeout_s``
        in all; then evaluates anyway.
        Media, images, fonts and video CDN requests are aborted, so the tab costs
        little next to the stream. The tab is never taken as the dashboard, a
        popout or an adopted window, and is closed even when something fails.
        """
        session = await self._browser_session()
        marker = f"#{_READ_MARKER}-{next(_READ_IDS)}"
        await session.send("Target.createTarget", {"url": "about:blank" + marker, "background": True})
        page = await self._wait_for_page("about:blank", self.fallback_wait_s, contains=marker)
        if page is None:
            raise BrowserError("the background tab did not show up")
        self._helper_pages.append(page)
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s
            await page.route("**/*", _abort_heavy)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            try:
                await page.wait_for_selector(ready_selector, timeout=timeout_s * 1000)
            except Exception as exc:  # Playwright's TimeoutError: nothing matched (yet)
                log.debug("background read: %s not found: %r", ready_selector, exc)
            else:
                count, stable_since = -1, loop.time()
                while loop.time() < deadline:
                    n = await page.locator(ready_selector).count()
                    if n != count:
                        count, stable_since = n, loop.time()
                    elif loop.time() - stable_since >= settle_s:
                        break
                    await asyncio.sleep(min(0.25, settle_s))
            return await page.evaluate(script)
        finally:
            try:
                await asyncio.wait_for(page.close(), CLOSE_TIMEOUT_S)
            except Exception as exc:
                log.warning("closing the background tab failed: %r", exc)
            if page in self._helper_pages:
                self._helper_pages.remove(page)

    async def find_page(
        self, url_prefix: str | None = None, *, contains: str | None = None
    ) -> "Page | None":
        """First open page whose URL starts with ``url_prefix`` and/or contains ``contains``."""
        if url_prefix is None and contains is None:
            raise ValueError("find_page needs url_prefix or contains")
        for page in self._open_pages():
            url = page.url or ""
            if url_prefix is not None and not url.startswith(url_prefix):
                continue
            if contains is not None and contains not in url:
                continue
            return page
        return None

    def _adopt_new_page(self, before: set) -> "Page | None":
        """A page that appeared since ``before`` (not extension/devtools/tracked), if any.

        Used when a window we asked for exists but its URL does not match what we
        expected (a redirect): reusing it beats opening yet another window.
        """
        for page in self._open_pages():
            url = page.url or ""
            if (
                page in before
                or page in self.popout_pages
                or page in self._helper_pages
                or url.startswith(_NOT_ADOPTABLE_SCHEMES)
            ):
                continue
            return page
        return None

    async def _wait_for_page(
        self,
        prefix: str,
        timeout_s: float,
        exited_badly: Callable[[], bool] | None = None,
        contains: str | None = None,
    ) -> "Page | None":
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            page = await self.find_page(prefix, contains=contains)
            if page is not None:
                return page
            if loop.time() >= deadline or (exited_badly is not None and exited_badly()):
                return None
            await asyncio.sleep(self.poll_s)

    async def open_app_window(self, url: str, timeout_s: float = 20) -> "Page":
        """Open ``url`` in its own Chromium app window and return its page.

        Preferred path: run ``chromium --user-data-dir=<profile> --app=<url>``,
        which hands off to the running instance. If no matching page shows up
        within ``timeout_s`` (or the spawn fails) a new window is opened over
        CDP instead (WARNING logged): the page must exist either way.
        """
        _ = self.context  # BrowserNotConnected if not connected
        prefix = _origin_path_prefix(url)
        # A hash marks the window as ours: another tab may show the same page without it.
        fragment = urlsplit(url).fragment
        marker = f"#{fragment}" if fragment else None
        page = await self.find_page(prefix, contains=marker)
        if page is not None:
            self.track_popout(page)
            return page

        before = set(self._open_pages())
        argv = [self.cfg.chromium_bin, f"--user-data-dir={self.cfg.user_data_dir}", f"--app={url}"]
        proc: Any = None
        try:
            proc = await self._spawn(argv)
        except Exception as exc:
            log.warning("starting the app window process failed: %r", exc)
            timeout_s = 0.0

        def exited_badly() -> bool:
            # The handoff process normally exits 0 once it passed the URL on; a
            # non-zero exit (e.g. headless Chromium refusing the handoff) means no page is coming.
            if not isinstance(proc, asyncio.Future) or not proc.done() or proc.cancelled():
                return False
            return proc.exception() is not None or proc.result() != 0

        page = await self._wait_for_page(prefix, timeout_s, exited_badly, contains=marker)
        if page is None:
            page = self._adopt_new_page(before)
            if page is not None:
                log.warning("app window opened with an unexpected URL (redirect?); using it")
        if page is None:
            log.warning(
                "app-window handoff produced no page (waited up to %.0fs); falling back to a CDP window",
                timeout_s,
            )
            page = await self._open_over_cdp(url, prefix, marker)
        self.track_popout(page)
        return page

    async def _open_over_cdp(self, url: str, prefix: str, marker: str | None = None) -> "Page":
        try:
            before = set(self._open_pages())
            session = await self._browser_session()
            await session.send("Target.createTarget", {"url": url, "newWindow": True})
            page = await self._wait_for_page(prefix, self.fallback_wait_s, contains=marker)
            if page is None:
                page = self._adopt_new_page(before)
                if page is not None:
                    log.warning("CDP window opened with an unexpected URL (redirect?); using it")
            if page is not None:
                return page
            log.warning("CDP window did not show up; opening a plain page instead")
        except BrowserNotConnected:
            raise
        except Exception as exc:
            log.warning("Target.createTarget failed (%r); opening a plain page instead", exc)
        try:
            page = await self.context.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            return page
        except BrowserNotConnected:
            raise
        except Exception as exc:
            raise BrowserError(f"cannot open the comments window: {type(exc).__name__}: {exc}") from exc

    # ---- SSN background page ------------------------------------------------

    async def refresh_ssn_background(self) -> int:
        """Replace the SSN extension's own background tab(s) with a fresh one.

        That tab can get stuck loading; SSN then fails every recovery attempt and
        posts "Social Stream background stopped" every minute. Chromium refuses
        to attach CDP to policy-installed extension pages, so the tab cannot be
        reloaded; closing it is allowed. SSN reopens it only when one of its
        scripts messages it, which may not happen during a live (the dashboard is
        parked), so if it has not reopened within ``ssn_reopen_wait_s`` the page
        is opened here (SSN adopts any open copy of it). Returns how many tabs
        were closed.
        """
        session = await self._browser_session()
        want = f"chrome-extension://{SSN_EXTENSION_ID}/{SSN_BACKGROUND_PAGE}"

        async def background_tabs() -> list[dict]:
            infos = (await session.send("Target.getTargets")).get("targetInfos", [])
            return [
                i for i in infos
                if i.get("type") == "page" and re.split(r"[?#]", i.get("url") or "")[0] == want
            ]

        closed = 0
        for info in await background_tabs():
            await session.send("Target.closeTarget", {"targetId": info["targetId"]})
            closed += 1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ssn_reopen_wait_s
        while not await background_tabs():
            if loop.time() >= deadline:
                await session.send("Target.createTarget", {"url": want, "background": True})
                log.info("closed %d SSN background tab(s); SSN did not reopen it, so it was opened", closed)
                return closed
            await asyncio.sleep(self.poll_s)
        log.info("closed %d SSN background tab(s); SSN reopened it", closed)
        return closed

    async def ensure_ssn_tabs(self) -> list[str]:
        """Open SSN's helper and background tabs if they are gone (e.g. the host closed
        them); a tab that is open is left alone. Returns what was opened."""
        if ssn_helper_url(self.cfg.user_data_dir) is None:
            return []  # the extension is not installed
        session = await self._browser_session()
        opened = []
        background = f"chrome-extension://{SSN_EXTENSION_ID}/{SSN_BACKGROUND_PAGE}"
        if not await self._ssn_helper_open(session, background):
            await session.send("Target.createTarget", {"url": background, "background": True})
            opened.append("background")
        helper = ssn_helper_url(self.cfg.user_data_dir)
        if helper and not await self._ssn_helper_open(session, helper):
            await self._open_ssn_helper()
            opened.append("helper")
        if opened:
            log.warning("SSN's %s tab(s) were closed; opened them again", " and ".join(opened))
        return opened

    # ---- SSN helper tab -----------------------------------------------------

    async def _open_ssn_helper(self) -> None:
        """Best effort: keep the SSN extension's helper page open in a background tab."""
        try:
            url = ssn_helper_url(self.cfg.user_data_dir)
            if url is None:
                log.debug("SSN extension not found in %s; no helper tab", self.cfg.user_data_dir)
                return
            session = await self._browser_session()
            if await self._ssn_helper_open(session, url):
                return
            before = set(self._open_pages())
            await session.send("Target.createTarget", {"url": url, "background": True})
            log.info("opened the SSN helper tab")
            # Remember its page so the app-window adoption never takes it (its URL may
            # still be blank, or chrome-error:// if the extension fails to load).
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.helper_wait_s
            while loop.time() < deadline:
                new = [p for p in self._open_pages() if p not in before]
                if new:
                    self._helper_pages.extend(new)
                    break
                await asyncio.sleep(self.poll_s)
        except Exception as exc:
            log.warning("could not open the SSN helper tab: %r", exc)

    @staticmethod
    async def _ssn_helper_open(session: Any, url: str) -> bool:
        """True if a tab already shows the helper page.

        Asks CDP rather than ``context.pages``: Playwright leaves chrome-extension://
        tabs out of it, so every reconnect used to add another helper tab. The path
        must match exactly, since SSN's own background.html is also a page target.
        """
        try:
            targets = (await session.send("Target.getTargets")).get("targetInfos", [])
        except Exception as exc:
            log.warning("cannot list tabs, opening the SSN helper anyway: %r", exc)
            return False
        want = url.split("#")[0].split("?")[0]
        return any(
            t.get("type") == "page" and (t.get("url") or "").split("#")[0].split("?")[0] == want
            for t in targets
        )

    # ---- network ------------------------------------------------------------

    async def http_reachable(self, url: str, timeout_s: float = 5) -> bool:
        """True if ``url`` answers with an HTTP status below 500 (network errors and timeouts: False)."""

        def probe() -> bool:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "stream-pilot"})
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    return resp.status < 500
            except urllib.error.HTTPError as exc:
                return exc.code < 500
            except Exception:  # URLError, timeout, ssl, refused...
                return False

        try:
            return await asyncio.wait_for(asyncio.to_thread(probe), timeout_s + 1)
        except (asyncio.TimeoutError, TimeoutError):
            return False
