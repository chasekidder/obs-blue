"""Facebook Live Producer flows on a Playwright ``Page``.

Everything is located by role or text through the patterns in
``fb.selectors`` (never pixels or obfuscated classes). Role locators are
preferred; a text locator is only used when no role locator matches. Playwright
is only imported for typing: the caller hands in an already-connected page.

Failure handling: each named step runs under ``step_timeout_s``; a timeout or
any Playwright error becomes ``StepFailed(step, short detail)``. ``go_live`` and
``wait_preview_ready`` are bounded as a whole (their timeout plus a small
grace), and the quick queries ``is_login_page`` / ``preview_lost`` are bounded
by a few seconds, so a frozen renderer can never hang the caller. Nothing here
blocks the event loop (no ``time.sleep``).

Secrets: the stream key is never logged (only that it was found and its
length). Note that ``record_dir`` screenshots/aria snapshots and
``save_failure`` output are raw page captures and may contain the key on the
setup page: treat those directories as secret (they are created 0700, files
0600).

Recording: ``record_dir`` captures are numbered in the order they are taken
(``NN-step``). The counter keeps running across ``setup()`` retries on the same
``Producer``, so a retry continues at the next number instead of overwriting.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlparse

from stream_pilot.fb import selectors as S
from stream_pilot.ports import StepFailed, StreamTarget

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

log = logging.getLogger(__name__)

_POLL_S = 0.25
_CAPTURE_TIMEOUT_MS = 10_000
_MAX_CONFIRM_CLICKS = 3
_ADVANCED_GRACE_S = 1.0  # how long the switch gets to appear before Advanced settings is clicked
_GRACE_S = 5.0  # added to go_live / wait_preview_ready timeouts for the outer bound
_QUERY_TIMEOUT_S = 5.0  # bound for is_login_page / preview_lost
_RECHECK_S = 1.0  # preview_lost: a disabled Go live must still be disabled this much later
_TEXT_FALLBACK_AFTER_S = 0.5  # text locators are tried only after role locators had this long
_CONFIRM_GONE_S = 2.0  # how long a clicked interstitial confirm gets to disappear

# One evaluate: input/textarea values + text of candidate elements. Returns a flat list of strings.
# Both passes are capped at ``limit`` nodes and every string is sliced to 500 chars.
_COLLECT_JS = """([valueSel, textSel, limit]) => {
  const out = [];
  let v = 0;
  for (const el of document.querySelectorAll(valueSel)) {
    if (++v > limit) break;
    out.push((el.value || '').slice(0, 500));
  }
  let n = 0;
  for (const el of document.querySelectorAll(textSel)) {
    if (++n > limit) break;
    out.push(((el.innerText || el.textContent) || '').slice(0, 500));
  }
  return out;
}"""


async def record_page(page: Any, directory: Path, name: str) -> None:
    """Best-effort ``<name>.png`` + ``<name>.yml`` (screenshot and aria snapshot) into ``directory``."""
    try:
        _private_dir(directory)
    except OSError as exc:
        log.warning("record %s: cannot create directory: %s", name, _short(exc))
        return
    stem = directory / name
    try:
        png = await page.screenshot(timeout=_CAPTURE_TIMEOUT_MS)
        _write_private(stem.with_suffix(".png"), png)
    except Exception as exc:
        log.warning("record %s: screenshot failed: %s", name, _short(exc))
    try:
        aria = await page.locator("body").aria_snapshot(timeout=_CAPTURE_TIMEOUT_MS)
        _write_private(stem.with_suffix(".yml"), aria)
    except Exception as exc:
        log.warning("record %s: aria snapshot failed: %s", name, _short(exc))


def is_login_flow_url(url: str) -> bool:
    """True if ``url`` is a step of Facebook's login flow (login page, 2FA, checkpoint)."""
    try:
        path = urlparse(url).path
    except ValueError:
        return False
    return path.startswith(S.LOGIN_FLOW_PATH_PREFIXES)


def _short(exc: BaseException) -> str:
    """One-line, bounded description of an error."""
    line = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {line}"[:200]


def _ms(seconds: float) -> float:
    return max(1.0, seconds * 1000.0)


def _write_private(path: Path, data: str | bytes) -> None:
    """Write ``data`` (text as UTF-8) to ``path`` with mode 0600, even if the file already exists."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(raw)
    finally:
        if fd >= 0:
            os.close(fd)


def _private_dir(path: Path) -> None:
    """Create ``path`` (and parents) and make sure the leaf is 0700."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


class Producer:
    def __init__(
        self,
        page: "Page",
        *,
        group_name: str,
        step_timeout_s: float,
        record_dir: Path | None = None,
    ):
        self._page = page
        self._group = S.group_name_pattern(group_name)  # ValueError on an empty name
        self._timeout_s = step_timeout_s
        self._record_dir = Path(record_dir) if record_dir else None
        self._record_n = 0
        self._skip_logged = 0  # last count of skipped required non-text controls that was logged

    # ---- locators (all patterns come from fb.selectors) ----------------------

    async def _locate(
        self, roles: tuple[str, ...], rx: re.Pattern, *, text: bool = False
    ) -> "Locator | None":
        """First visible match, trying each role in order and text only if no role matches. No waiting."""
        for role in roles:
            loc = self._page.get_by_role(role, name=rx).filter(visible=True)  # type: ignore[arg-type]
            if await loc.count():
                return loc.first
        if text:
            loc = self._page.get_by_text(rx).filter(visible=True)
            if await loc.count():
                return loc.first
        return None

    async def _find(
        self,
        step: str,
        roles: tuple[str, ...],
        rx: re.Pattern,
        *,
        text: bool = False,
        timeout_s: float,
    ) -> "Locator":
        """Poll ``_locate`` until something matches; ``StepFailed(step)`` at the deadline."""
        start = self._now()
        deadline = start + timeout_s
        text_after = start + min(_TEXT_FALLBACK_AFTER_S, timeout_s / 2)
        last_err = ""
        while True:
            try:
                loc = await self._locate(roles, rx, text=text and self._now() >= text_after)
                if loc is not None:
                    return loc
            except Exception as exc:  # e.g. mid-navigation; keep polling until the deadline
                last_err = _short(exc)
            if self._now() >= deadline:
                detail = f"not found within {timeout_s:g}s"
                raise StepFailed(step, detail + (f" (last error: {last_err})" if last_err else ""))
            await asyncio.sleep(0.1)

    # ---- step plumbing --------------------------------------------------------

    async def _step(self, name: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await asyncio.wait_for(fn(), self._timeout_s)
        except StepFailed:
            raise
        except TimeoutError:
            raise StepFailed(name, f"timed out after {self._timeout_s:g}s") from None
        except Exception as exc:  # Playwright errors (timeouts, closed page, detached ...)
            raise StepFailed(name, _short(exc)) from exc

    @staticmethod
    async def _bounded(
        name: str, aw: Awaitable[Any], limit_s: float, what: str, *, wrap: bool = False
    ) -> Any:
        """``aw`` under ``limit_s``; expiry -> ``StepFailed(name)``. ``wrap``: other errors too."""
        try:
            return await asyncio.wait_for(aw, limit_s)
        except StepFailed:
            raise
        except TimeoutError:
            raise StepFailed(name, f"{what} timed out after {limit_s:g}s (page unresponsive?)") from None
        except Exception as exc:
            if not wrap:
                raise
            raise StepFailed(name, _short(exc)) from exc

    def _deadline(self, fraction: float = 0.8) -> float:
        return asyncio.get_running_loop().time() + self._timeout_s * fraction

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    @staticmethod
    def _note_error(exc: BaseException, ctx: str, seen: set[str]) -> str:
        """Short text of ``exc``; WARNING once per distinct error (digits ignored)."""
        text = _short(exc)
        key = re.sub(r"\d+", "#", text)
        if key not in seen:
            seen.add(key)
            log.warning("%s: %s", ctx, text)
        return text

    async def _is_checked(self, loc: "Locator") -> bool | None:
        """True/False from ``aria-checked`` or the native checked state; ``None`` when unknown."""
        attr = await loc.get_attribute(S.ATTR_ARIA_CHECKED, timeout=2000)
        if attr == "true":
            return True
        if attr == "false":
            return False
        if attr is not None:  # e.g. "mixed"
            return None
        try:
            return await loc.is_checked(timeout=2000)
        except Exception:
            return None

    async def _record(self, step: str) -> None:
        """Best-effort: ``NN-step.png`` + ``NN-step.yml`` (the live-runbook recorder)."""
        if self._record_dir is None:
            return
        self._record_n += 1
        await record_page(self._page, self._record_dir, f"{self._record_n:02d}-{step}")

    # ---- simple queries -------------------------------------------------------

    async def open(self, url: str) -> None:
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=_ms(self._timeout_s))
        except Exception as exc:
            raise StepFailed("open", _short(exc)) from exc

    async def is_login_page(self) -> bool:
        """True if the login form is visible. Errors raise ``StepFailed("login_check")``."""

        async def query() -> bool:
            return await self._page.locator(S.LOGIN_FORM_SELECTOR).first.is_visible()

        return await self._bounded("login_check", query(), _QUERY_TIMEOUT_S, "login check", wrap=True)

    async def in_login_flow(self) -> bool:
        """True while Facebook's login form or a later login step (2FA) is on screen.

        Never navigates: the host may be typing into it.
        """
        if is_login_flow_url(self._page.url or ""):
            return True
        return await self.is_login_page()

    async def preview_lost(self) -> bool:
        """True if the preview-lost text is visible, or the Go live button is no longer
        enabled (checked twice, ``_RECHECK_S`` apart, so a flicker is not a loss).
        Errors raise ``StepFailed("preview_lost")``."""

        async def go_live_enabled() -> bool:
            btn = await self._go_live_button()
            if btn is None or not await btn.is_enabled(timeout=1000):
                return False
            return await btn.get_attribute(S.ATTR_ARIA_DISABLED, timeout=1000) != "true"

        async def query() -> bool:
            loc = self._page.get_by_text(S.PREVIEW_LOST_TEXT).filter(visible=True).first
            if await loc.is_visible():
                return True
            if await go_live_enabled():
                return False
            await asyncio.sleep(_RECHECK_S)
            return not await go_live_enabled()

        return await self._bounded("preview_lost", query(), _QUERY_TIMEOUT_S, "preview check", wrap=True)

    # ---- setup (spec "Facebook setup" steps 1-5) ------------------------------

    async def setup(self) -> StreamTarget:
        """Group check, Go live, Streaming software, persistent key, then read server and key."""
        await self._run("group", self._step_group)
        await self._run("go_live_start", self._step_go_live_start)
        await self._run("streaming_software", self._step_streaming_software)
        await self._run("advanced", self._step_advanced)
        await self._run("persistent_key", self._step_persistent_key)
        await self._step_end_on_stop()
        await self._record("end_on_stop")
        target = await self._run("read_key", self._step_read_key)
        return target

    async def _run(self, name: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        result = await self._step(name, fn)
        await self._record(name)
        return result

    async def _step_group(self) -> None:
        await self._page.get_by_text(self._group).filter(visible=True).first.wait_for(
            state="visible", timeout=_ms(self._timeout_s)
        )

    async def _step_go_live_start(self) -> None:
        btn = await self._find(
            "go_live_start", (S.ROLE_BUTTON,), S.GO_LIVE_START, timeout_s=self._timeout_s * 0.4
        )
        await btn.click(timeout=_ms(self._timeout_s * 0.5))

    async def _step_streaming_software(self) -> None:
        loc = await self._find(
            "streaming_software",
            (S.ROLE_RADIO, S.ROLE_BUTTON),
            S.STREAMING_SOFTWARE,
            text=True,
            timeout_s=self._timeout_s * 0.4,
        )
        await loc.click(timeout=_ms(self._timeout_s * 0.5))

    async def _switch(self, step: str, rx: re.Pattern, timeout_s: float) -> "Locator":
        return await self._find(step, (S.ROLE_SWITCH, S.ROLE_CHECKBOX), rx, timeout_s=timeout_s)

    async def _step_advanced(self) -> None:
        """Open Advanced settings unless the switch is already there or the panel is already open."""
        quarter = self._timeout_s / 4
        try:
            await self._switch("advanced", S.PERSISTENT_KEY, min(_ADVANCED_GRACE_S, quarter))
            return  # already visible: Advanced settings is open (or not needed)
        except StepFailed:
            pass
        adv = await self._find(
            "advanced", (S.ROLE_BUTTON,), S.ADVANCED_SETTINGS, text=True, timeout_s=quarter
        )
        if await adv.get_attribute(S.ATTR_ARIA_EXPANDED, timeout=2000) == "true":
            # Open already (the switch is just slow): clicking would collapse it. persistent_key waits.
            log.info("advanced: panel already open; not clicking")
            return
        await adv.click(timeout=_ms(quarter))
        try:
            await self._switch("advanced", S.PERSISTENT_KEY, quarter)
        except StepFailed:
            raise StepFailed(
                "advanced", "persistent key setting not shown after clicking Advanced settings"
            ) from None

    async def _step_persistent_key(self) -> None:
        # budgets: find 0.4 + click 0.3 + verify 0.3 = 1.0 of the step timeout
        key = await self._switch("persistent_key", S.PERSISTENT_KEY, self._timeout_s * 0.4)
        state = await self._is_checked(key)
        if state is None:
            raise StepFailed("persistent_key", "cannot determine switch state")
        if state:
            return
        await key.click(timeout=_ms(self._timeout_s * 0.3))
        deadline = self._now() + self._timeout_s * 0.3
        while True:
            state = await self._is_checked(key)
            if state:
                return
            if self._now() >= deadline:
                shown = "unknown" if state is None else "off"
                raise StepFailed("persistent_key", f"switch did not turn on (state: {shown})")
            await asyncio.sleep(0.1)

    async def _step_end_on_stop(self) -> None:
        """Soft: never raises. Absent -> INFO; present and off -> turn on; errors -> WARNING.

        Bounded by the step timeout, so a hung page can't stall setup().
        """
        try:
            await asyncio.wait_for(self._end_on_stop_impl(), self._timeout_s)
        except Exception as exc:
            log.warning("end_on_stop: could not check/enable: %s", _short(exc))

    async def _end_on_stop_impl(self) -> None:
        sw = await self._locate((S.ROLE_SWITCH, S.ROLE_CHECKBOX), S.END_LIVE_WHEN_STREAM_STOPS)
        if sw is None:
            log.info("end_on_stop: setting not shown on the setup page; skipping")
            return
        state = await self._is_checked(sw)
        if state is None:
            log.warning("end_on_stop: cannot determine switch state; leaving it alone")
            return
        if state:
            log.info("end_on_stop: already on")
            return
        await sw.click(timeout=_ms(self._timeout_s / 2))
        deadline = self._deadline(0.4)
        while not await self._is_checked(sw):
            if self._now() >= deadline:
                log.warning("end_on_stop: switch did not turn on")
                return
            await asyncio.sleep(0.1)
        log.info("end_on_stop: turned on")

    async def _step_read_key(self) -> StreamTarget:
        """Poll the page (every 0.25 s, up to 80% of the step timeout) for the server URL and key.

        Each poll is one ``page.evaluate`` over ``input, textarea`` values and the text of
        ``[data-testid], code, span``, both capped at 2000 nodes and 500 chars per value. The key may
        render late, and navigation can destroy the evaluation context mid-poll: such evaluate errors
        are remembered and retried until the deadline (then reported in the detail).
        """
        deadline = self._deadline(0.8)
        last_err = ""
        key = server = None
        while True:
            try:
                values: list[str] = await self._page.evaluate(
                    _COLLECT_JS,
                    [S.VALUE_ELEMENTS_SELECTOR, S.TEXT_ELEMENTS_SELECTOR, S.MAX_TEXT_ELEMENTS],
                )
            except Exception as exc:  # e.g. "Execution context was destroyed" during navigation
                last_err = _short(exc)
            else:
                key = S.pick_stream_key(values)
                server = S.pick_server_url(values)
                if key and server:
                    log.info("read_key: server url and stream key found (key length %d)", len(key))
                    return StreamTarget(server=server, key=key)
            if self._now() >= deadline:
                missing = " and ".join(n for n, v in (("stream key", key), ("server url", server)) if not v)
                detail = f"{missing or 'stream key and server url'} not found on the page"
                if last_err:
                    detail += f" (last error: {last_err})"
                raise StepFailed("read_key", detail)
            await asyncio.sleep(_POLL_S)

    # ---- preview and going live ----------------------------------------------

    async def wait_preview_ready(self, timeout_s: float) -> None:
        """Wait until the Go live button is enabled (not disabled, not aria-disabled).

        Bounded as a whole: ``timeout_s`` plus a grace, then ``StepFailed("preview")``.
        """
        await self._bounded(
            "preview",
            self._wait_preview_ready_impl(timeout_s),
            timeout_s + _GRACE_S,
            f"waiting for the preview ({timeout_s:g}s budget)",
        )

    async def _wait_preview_ready_impl(self, timeout_s: float) -> None:
        deadline = self._now() + timeout_s
        last_err = ""
        seen: set[str] = set()
        button_seen = False
        while True:
            try:
                btn = await self._go_live_button()
                button_seen = button_seen or btn is not None
                if btn is not None and await btn.is_enabled(timeout=1000):
                    if await btn.get_attribute(S.ATTR_ARIA_DISABLED, timeout=1000) != "true":
                        return
            except Exception as exc:  # e.g. mid-navigation; keep polling until the deadline
                last_err = self._note_error(exc, "preview poll", seen)
            if self._now() >= deadline:
                detail = f"go-live button not enabled within {timeout_s:g}s (button_seen={button_seen}"
                detail += f", last error: {last_err})" if last_err else ")"
                raise StepFailed("preview", detail)
            await asyncio.sleep(_POLL_S)

    async def set_title(self, title: str) -> None:
        """Post details: open the composer, type ``title`` into its title box, save."""
        await self._step("title", lambda: self._set_title(title))

    async def _set_title(self, title: str) -> None:
        button = await self._find(
            "title", (S.ROLE_BUTTON,), S.POST_DETAILS_BUTTON, timeout_s=self._timeout_s * 0.4
        )
        await button.click(timeout=5000)
        box_rx = self._page.get_by_role(S.ROLE_TEXTBOX, name=S.POST_TITLE_FIELD)  # type: ignore[arg-type]
        dialog = self._page.get_by_role(S.ROLE_DIALOG).filter(has=box_rx).filter(visible=True).first  # type: ignore[arg-type]
        box = dialog.get_by_role(S.ROLE_TEXTBOX, name=S.POST_TITLE_FIELD).first  # type: ignore[arg-type]
        await box.fill(title, timeout=5000)
        await dialog.get_by_role(S.ROLE_BUTTON, name=S.POST_DETAILS_SAVE).first.click(timeout=5000)  # type: ignore[arg-type]
        await box.wait_for(state="hidden", timeout=5000)
        log.info("title: set (%d characters)", len(title))
        await self._record("title")

    async def go_live(self, timeout_s: float) -> str:
        """Click Go live, clear any interstitial, wait for End live video; return the live id.

        Bounded as a whole: ``timeout_s`` plus a grace, then ``StepFailed("go_live")``.
        """
        return await self._bounded(
            "go_live",
            self._go_live_impl(timeout_s),
            timeout_s + _GRACE_S,
            f"going live ({timeout_s:g}s budget)",
        )

    async def _go_live_impl(self, timeout_s: float) -> str:
        deadline = self._now() + timeout_s
        try:
            btn = await self._find_go_live_button(timeout_s * 0.25)
            # at most half the budget: the interstitial and the end-live wait need the rest
            await btn.click(timeout=_ms(timeout_s * 0.5))
        except StepFailed:
            raise
        except Exception as exc:
            raise StepFailed("go_live", f"go-live button not clickable: {_short(exc)}") from exc

        await self._record("go_live_clicked")
        state = {"dialog_seen": False, "confirms": 0}
        last_err = ""
        seen: set[str] = set()
        while True:
            try:
                if await self._end_live_visible():
                    break
                if state["confirms"] < _MAX_CONFIRM_CLICKS:
                    await self._confirm_interstitial(deadline, state)
            except Exception as exc:  # navigation in flight, click timeout etc.; retry until the deadline
                last_err = self._note_error(exc, "go_live poll", seen)
            if self._now() >= deadline:
                detail = (
                    f"end-live control not shown within {timeout_s:g}s "
                    f"(dialog_seen={state['dialog_seen']} confirms={state['confirms']}"
                )
                detail += f", last error: {last_err})" if last_err else ")"
                raise StepFailed("go_live", detail)
            await asyncio.sleep(_POLL_S)

        # the URL can lag the page by a moment: allow a short grace beyond the deadline
        grace = max(deadline, self._now() + 2.0)
        while True:
            live_id = S.live_id_from_url(self._page.url)
            if live_id:
                log.info("go_live: live id %s", live_id)
                await self._record("go_live")
                return live_id
            if self._now() >= grace:
                raise StepFailed("go_live", "end-live control visible but no live id in URL (a live IS running)")
            await asyncio.sleep(_POLL_S)

    async def _go_live_button(self) -> "Locator | None":
        """The real Go live button (not the step tracker's; see ``S.GO_LIVE_IS_LAST_MATCH``). No waiting."""
        loc = self._page.get_by_role(S.ROLE_BUTTON, name=S.GO_LIVE).filter(visible=True)  # type: ignore[arg-type]
        if not await loc.count():
            return None
        return loc.last if S.GO_LIVE_IS_LAST_MATCH else loc.first

    async def _find_go_live_button(self, timeout_s: float) -> "Locator":
        """Poll :meth:`_go_live_button` until it exists; ``StepFailed("go_live")`` at the deadline."""
        deadline = self._now() + timeout_s
        last_err = ""
        while True:
            try:
                btn = await self._go_live_button()
                if btn is not None:
                    return btn
            except Exception as exc:  # e.g. mid-navigation; keep polling until the deadline
                last_err = _short(exc)
            if self._now() >= deadline:
                detail = f"not found within {timeout_s:g}s"
                raise StepFailed("go_live", detail + (f" (last error: {last_err})" if last_err else ""))
            await asyncio.sleep(0.1)

    async def _end_live_visible(self) -> bool:
        return await self._locate((S.ROLE_BUTTON,), S.END_LIVE_VIDEO, text=True) is not None

    async def _confirm_interstitial(self, deadline: float, state: dict[str, Any]) -> bool:
        """If a dialog with a confirm button is showing, fill a required title and confirm.

        True if the confirm was clicked. After a click the dialog gets up to ``_CONFIRM_GONE_S`` to go
        away, so a still-visible dialog is not clicked again in quick succession.
        """
        dialog = self._page.get_by_role(S.ROLE_DIALOG).filter(visible=True).first  # type: ignore[arg-type]
        if not await dialog.count():
            return False
        state["dialog_seen"] = True
        confirm = dialog.get_by_role(S.ROLE_BUTTON, name=S.INTERSTITIAL_CONFIRM).first  # type: ignore[arg-type]
        if not await confirm.count():
            return False
        await self._fill_required_title(dialog)
        remaining = max(1.0, deadline - self._now())
        await confirm.click(timeout=_ms(min(remaining, 5.0)))
        state["confirms"] += 1
        try:
            await confirm.wait_for(state="hidden", timeout=_ms(min(_CONFIRM_GONE_S, remaining)))
        except Exception:  # still there (or the page is navigating): the poll loop decides
            pass
        return True

    async def _fill_required_title(self, dialog: "Locator") -> None:
        """A required/title *textbox* left empty blocks the confirm button: give it a default title.

        Only textboxes are ever typed into; any other required control is skipped (and logged).
        """
        title_boxes = dialog.get_by_role(S.ROLE_TEXTBOX, name=S.TITLE_FIELD)  # type: ignore[arg-type]
        required_boxes = dialog.get_by_role(S.ROLE_TEXTBOX).and_(dialog.locator(S.REQUIRED_FIELD_SELECTOR))  # type: ignore[arg-type]
        fields = title_boxes.or_(required_boxes).filter(visible=True)
        for i in range(await fields.count()):
            field = fields.nth(i)
            if not await self._field_text(field):
                await field.fill(S.DEFAULT_LIVE_TITLE, timeout=3000)
                log.warning("go_live: filled required title field with default")
        required_total = await dialog.locator(S.REQUIRED_FIELD_SELECTOR).filter(visible=True).count()
        required_text = await required_boxes.filter(visible=True).count()
        if required_total > required_text:
            skipped = required_total - required_text
            if skipped != self._skip_logged:  # once per distinct situation, not on every poll
                self._skip_logged = skipped
                log.warning("go_live: skipping %d required non-text control(s) in the dialog", skipped)

    @staticmethod
    async def _field_text(field: "Locator") -> str:
        try:
            return (await field.input_value(timeout=2000)).strip()
        except Exception:  # contenteditable textbox
            return ((await field.text_content(timeout=2000)) or "").strip()

    # ---- diagnostics ----------------------------------------------------------

    async def save_failure(self, step: str, directory: Path) -> None:
        """Write screenshot.png, page.html and aria.yml; each best-effort, never raises."""
        directory = Path(directory)
        try:
            _private_dir(directory)
        except Exception as exc:
            log.warning("save_failure(%s): cannot create %s: %s", step, directory, _short(exc))
            return
        try:
            png = await self._page.screenshot(full_page=True, timeout=_CAPTURE_TIMEOUT_MS)
            _write_private(directory / "screenshot.png", png)
        except Exception as exc:
            log.warning("save_failure(%s): screenshot failed: %s", step, _short(exc))
        try:
            _write_private(directory / "page.html", await self._page.content())
        except Exception as exc:
            log.warning("save_failure(%s): page.html failed: %s", step, _short(exc))
        try:
            aria = await self._page.locator("body").aria_snapshot(timeout=_CAPTURE_TIMEOUT_MS)
            _write_private(directory / "aria.yml", aria)
        except Exception as exc:
            log.warning("save_failure(%s): aria snapshot failed: %s", step, _short(exc))
