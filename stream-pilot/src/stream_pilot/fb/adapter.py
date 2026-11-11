"""The real ``FacebookPort`` / ``CommentCapturePort`` adapters.

They compose the pieces built earlier: :class:`CdpBrowser` (connection, pages),
:class:`Producer` (Facebook Live Producer flows on the dashboard page),
:class:`FbDomCapture` (comments popout watcher) and :class:`SsnListener`
(chat websocket). Constructing them touches nothing; ``FacebookAdapter.connect``
(called by the controller) attaches to Chromium. Every browser-touching method
used before that raises :class:`BrowserNotConnected` (a clear ``RuntimeError``).

Facebook text/roles/CSS/URL patterns stay in ``fb/selectors.py``; this module
only formats the URL templates from the config.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from stream_pilot.browser import CdpBrowser
from stream_pilot.comment_log import CommentLog
from stream_pilot.config import Config
from stream_pilot.fb import selectors as S
from stream_pilot.fb.comments import FbDomCapture
from stream_pilot.fb.producer import Producer
from stream_pilot.ports import StreamTarget
from stream_pilot.ssn_listener import SsnListener

log = logging.getLogger(__name__)

_POPOUT_GRACE_S = 1.0  # how long start() looks for an already-open popout before opening one
_POLL_S = 0.1
_GOTO_BLANK_MIN_S = 5.0
_CLOSE_POPOUT_S = 3.0  # bound for closing one popout page in stop()


class FacebookAdapter:
    """``FacebookPort`` on the dashboard page of the shared browser."""

    def __init__(
        self, cfg: Config, browser: CdpBrowser, *, record_dir: Path | None = None
    ) -> None:
        self._cfg = cfg
        self._browser = browser
        self._record_dir = Path(record_dir) if record_dir else None
        self._producer: Producer | None = None
        self._producer_page = None
        self._comments_page = None

    async def _ensure_connected(self) -> None:
        """Reconnect after Chromium restarted (``connect`` is idempotent and serialized)."""
        if not self._browser.is_connected:
            # Shielded: callers run under short bounds (the controller's 5 s preview_lost
            # check); the reconnect must still finish in the background and not restart.
            await asyncio.shield(self._browser.connect())

    async def _producer_for_dashboard(self) -> Producer:
        """One Producer per dashboard page (a new page, e.g. after a restart, gets a new one)."""
        await self._ensure_connected()
        page = await self._browser.dashboard_page()
        if self._producer is None or self._producer_page is not page:
            self._producer = Producer(
                page,
                group_name=self._cfg.facebook.group_name,
                step_timeout_s=self._cfg.timeouts.step_s,
                record_dir=self._record_dir,
            )
            self._producer_page = page
        return self._producer

    # ---- FacebookPort -------------------------------------------------------

    async def connect(self) -> None:
        await self._browser.connect()

    async def reachable(self) -> bool:
        return await self._browser.http_reachable(S.REACHABILITY_URL)

    async def logged_in(self) -> bool:
        producer = await self._producer_for_dashboard()
        await producer.open(self._cfg.facebook.producer_url)
        return not await producer.is_login_page()

    async def setup_live(self) -> StreamTarget:
        return await (await self._producer_for_dashboard()).setup()

    async def wait_preview_ready(self, timeout_s: float) -> None:
        await (await self._producer_for_dashboard()).wait_preview_ready(timeout_s)

    async def preview_lost(self) -> bool:
        return await (await self._producer_for_dashboard()).preview_lost()

    async def go_live(self, timeout_s: float) -> str:
        return await (await self._producer_for_dashboard()).go_live(timeout_s)

    async def open_comments(self, live_id: str) -> None:
        await self._ensure_connected()
        url = self._cfg.facebook.comments_url_template.format(live_id=live_id)
        self._comments_page = await self._browser.open_app_window(url)

    async def park_dashboard(self) -> None:
        await self._ensure_connected()
        page = await self._browser.dashboard_page()
        timeout_ms = max(_GOTO_BLANK_MIN_S, self._cfg.timeouts.step_s) * 1000
        await page.goto("about:blank", timeout=timeout_ms)

    async def save_failure(self, step: str, directory: Path) -> None:
        await (await self._producer_for_dashboard()).save_failure(step, directory)

    async def close_browser(self) -> None:
        await self._browser.close_browser()


class CaptureAdapter:
    """``CommentCapturePort``: DOM capture of the comments popout plus the SSN chat listener.

    Calling :meth:`start` again for the same live (the controller does so when
    :meth:`healthy` turns False, e.g. after Chromium restarted) re-attaches to
    the popout, opening it if it is gone, and reuses the same ``CommentLog`` so
    de-duplication state carries over. The SSN listener runs once per stream.
    """

    def __init__(self, cfg: Config, browser: CdpBrowser) -> None:
        self._cfg = cfg
        self._browser = browser
        self._dom: FbDomCapture | None = None
        self._dom_context: object | None = None
        self._log: CommentLog | None = None
        self._ssn_task: asyncio.Task | None = None
        self._gen = 0  # bumped by stop(): a start() that began earlier must not finish

    @property
    def dom_capture(self) -> FbDomCapture | None:
        return self._dom

    def healthy(self) -> bool:
        """True iff the DOM capture is attached to a popout page that is still open."""
        dom = self._dom
        if dom is None or not self._browser.is_connected:
            return False
        page = dom.attached_page
        return page is not None and not page.is_closed()

    def _check_gen(self, gen: int) -> None:
        if gen != self._gen:
            raise asyncio.CancelledError("capture.stop() ran while capture.start() was in progress")

    async def start(self, live_id: str, stream_dir: Path) -> None:
        gen = self._gen
        if not self._browser.is_connected:
            await self._browser.connect()
        self._check_gen(gen)
        context = self._browser.context
        if self._dom is None or self._dom_context is not context:
            # One FbDomCapture per BrowserContext: the binding and init script are
            # installed once per context (a second instance would re-register them).
            self._dom = FbDomCapture(context)
            self._dom_context = context

        path = Path(stream_dir) / "comments.jsonl"
        if self._log is None or self._log.live_id != live_id or self._log.path != path:
            self._log = CommentLog(path, live_id)
        comment_log = self._log

        # The SSN chat feed is independent of the popout: start it first so a
        # missing popout never costs comments.
        self._start_ssn()
        page = await self._popout_page(live_id)
        if gen != self._gen:  # stop() ran meanwhile: leave nothing behind
            await self._close_page(page)
            self._check_gen(gen)
        await self._dom.attach(page, comment_log)
        if gen != self._gen:
            try:
                await self._dom.detach()
            finally:
                self._check_gen(gen)

    async def stop(self) -> None:
        """Detach and stop the SSN listener; the comments file is kept. Safe without a ``start``."""
        self._gen += 1  # any start() still in flight is now stale
        self._log = None  # the SSN listener drops messages from now on
        dom = self._dom
        if dom is not None:
            try:
                await dom.detach()
            except Exception:
                log.exception("detaching the comment capture failed")
        task, self._ssn_task = self._ssn_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
            if not task.cancelled():
                task.exception()  # mark retrieved
        await self._close_popouts()

    async def _close_popouts(self) -> None:
        """Close the comments popout window(s): no pile-up across stream cycles.

        A page is untracked only once it is closed; one that would not close stays
        tracked (so it is never mistaken for the dashboard) and is retried next time.
        """
        for page in list(self._browser.popout_pages):
            await self._close_page(page)

    async def _close_page(self, page) -> None:
        try:
            if not page.is_closed():
                await asyncio.wait_for(page.close(), _CLOSE_POPOUT_S)
        except Exception as exc:  # TimeoutError included; never raises
            log.warning("closing the comments popout failed: %r", exc)
            return
        self._browser.untrack_popout(page)

    # ---- helpers ------------------------------------------------------------

    async def _popout_page(self, live_id: str):
        fragment = S.popout_url_fragment(live_id)
        url = self._cfg.facebook.comments_url_template.format(live_id=live_id)
        grace = min(_POPOUT_GRACE_S, self._cfg.timeouts.step_s)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        while True:
            page = await self._browser.find_page(contains=fragment)
            if page is not None:
                self._browser.track_popout(page)
                return page
            page = self._redirected_popout()
            if page is not None:
                # A popout we opened earlier whose URL no longer matches (a redirect or
                # navigation away): reuse it instead of piling up another window.
                log.info("reusing the comments popout (URL does not match); navigating it back")
                try:
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self._cfg.timeouts.step_s * 1000,
                    )
                except Exception as exc:
                    log.warning("navigating the popout failed: %r", exc)
                return page
            if loop.time() >= deadline:
                break
            await asyncio.sleep(_POLL_S)
        log.info("comments popout for live %s not open; opening it", live_id)
        return await self._browser.open_app_window(url, timeout_s=self._cfg.timeouts.step_s)

    def _redirected_popout(self):
        """An open tracked popout page whose URL is not a comments popout URL (of any live).

        A tracked popout that IS a popout URL but belongs to another live is a stale
        window and is not reused.
        """
        for page in self._browser.open_popouts():
            if not S.is_popout_url(page.url or ""):
                return page
        return None

    def _start_ssn(self) -> None:
        ssn = self._cfg.ssn
        if not (ssn.enabled and ssn.session_id):
            return
        if self._ssn_task is not None and not self._ssn_task.done():
            return
        listener = SsnListener(ssn.session_id, lambda: self._log)
        task = asyncio.get_running_loop().create_task(listener.run(), name="ssn-listener")
        task.add_done_callback(self._ssn_done)
        self._ssn_task = task

    @staticmethod
    def _ssn_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # the session id is a credential: log the type only
            log.error("SSN listener stopped unexpectedly (%s)", type(exc).__name__)
