"""DOM comment capture: inject the watcher into the comments popout and feed the log.

``fb/watcher.js`` is a template (package data). It is rendered here from
``fb/selectors.py`` so Facebook's markup knowledge stays in one file, installed
as a context-wide init script, and reports through the Playwright binding
``__spComment``. The binding callback routes each message to the
:class:`CommentLog` registered for the page that sent it.

Playwright can neither unregister a binding nor an init script (and raises if
a binding name is registered twice), so both are installed once per
BrowserContext by a shared :class:`_Router`; :class:`FbDomCapture` is just a
handle onto the router of its context. :meth:`FbDomCapture.detach` only stops
routing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import weakref
from collections import Counter
from importlib import resources
from typing import TYPE_CHECKING, Any, Callable

from stream_pilot.comment_log import Comment, CommentLog
from stream_pilot.fb import selectors

if TYPE_CHECKING:  # Playwright is only installed in the image
    from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)

BINDING_NAME = "__spComment"
_PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")


def _placeholders() -> dict[str, str]:
    js_str = json.dumps  # a JS string literal (valid JS: ASCII-only escapes)
    return {
        "__ARTICLE_SELECTOR__": js_str(selectors.COMMENT_ARTICLE_SELECTOR),
        "__LABEL_ATTR__": js_str(selectors.COMMENT_LABEL_ATTR),
        "__LABEL_RE__": selectors.js_regex_source(selectors.COMMENT_LABEL_RE),
        "__COMMENT_ID_RE__": selectors.js_regex_source(selectors.COMMENT_ID_IN_HREF_RE),
        "__AUTHOR_LINK_SELECTOR__": js_str(selectors.COMMENT_AUTHOR_LINK_SELECTOR),
        "__PERMALINK_SELECTOR__": js_str(selectors.COMMENT_PERMALINK_SELECTOR),
        "__TIME_SELECTOR__": js_str(selectors.COMMENT_TIME_SELECTOR),
        "__TIME_UTIME_ATTR__": js_str(selectors.COMMENT_TIME_UTIME_ATTR),
        "__TIME_DATETIME_ATTR__": js_str(selectors.COMMENT_TIME_DATETIME_ATTR),
        "__COMMENTS_PATH__": js_str(selectors.COMMENTS_PATH_MARKER),
    }


def watcher_template() -> str:
    return resources.files("stream_pilot.fb").joinpath("watcher.js").read_text(encoding="utf-8")


def render_watcher(template: str | None = None) -> str:
    """The watcher script with every placeholder filled in from ``selectors``.

    Raises RuntimeError if the template uses an unknown placeholder.
    """
    script = watcher_template() if template is None else template
    for placeholder, value in _placeholders().items():
        script = script.replace(placeholder, value)
    left = sorted(set(_PLACEHOLDER_RE.findall(script)))
    if left:
        raise RuntimeError(f"watcher.js has unfilled placeholders: {', '.join(left)}")
    return script


def _opt_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _parse_items(items: Any) -> list[Comment]:
    if not isinstance(items, list):
        raise ValueError(f"comment batch items must be a list, got {type(items).__name__}")
    out: list[Comment] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append(
            Comment(
                author=str(it.get("author") or ""),
                text=str(it.get("text") or ""),
                fb_comment_id=_opt_str(it.get("fb_comment_id")),
                created_at=_opt_str(it.get("created_at")),
            )
        )
    return out


class _Router:
    """Per-context owner of the binding, the init script and the routing state.

    ``owner`` is the FbDomCapture handle currently attached through this router;
    it receives the health bookkeeping (counts / last_message_at).
    """

    def __init__(self, context: "BrowserContext") -> None:
        self._context = context
        self._installed = False
        self._lock = asyncio.Lock()
        self.owner: "FbDomCapture | None" = None
        self.page: "Page | None" = None
        self.log: CommentLog | None = None

    async def ensure_installed(self) -> None:
        async with self._lock:
            if self._installed:
                return
            await self._context.expose_binding(BINDING_NAME, self.on_message)
            await self._context.add_init_script(render_watcher())
            self._installed = True

    def route(self, owner: "FbDomCapture", page: "Page | None", log: CommentLog | None) -> None:
        self.owner, self.page, self.log = owner, page, log

    # Sync on purpose: Playwright runs it in message order. Never raises.
    def on_message(self, source: Any, payload: Any) -> None:
        try:
            page = source["page"]
            owner, target, log = self.owner, self.page, self.log
            if owner is None or target is None or log is None or page is not target:
                logger.debug("dropping comment message from a non-attached page")
                return
            msg = json.loads(payload)
            kind = msg.get("kind")
            if kind == "hello":
                owner._accept(kind)
                logger.info("comment watcher attached: %s", msg.get("href"))
            elif kind in ("rescan", "live"):
                items = _parse_items(msg.get("items"))
                owner._accept(kind)
                n = log.ingest_dom(items, rescan=(kind == "rescan"))
                logger.debug("%s batch of %d: %d new", kind, len(items), n)
            else:
                logger.debug("ignoring comment message kind %r", kind)
        except Exception:
            logger.exception("failed to ingest comment message")


_ROUTERS: "weakref.WeakKeyDictionary[Any, _Router]" = weakref.WeakKeyDictionary()


def _router_for(context: "BrowserContext") -> _Router:
    router = _ROUTERS.get(context)
    if router is None:
        router = _ROUTERS[context] = _Router(context)
    return router


class FbDomCapture:
    """Feeds one CommentLog from the comments popout page.

    A handle onto the per-context router: several instances may be created for
    the same context, and the one that attached last receives the messages
    (earlier handles are superseded and report ``attached_page`` None).

    Health-related attributes (for the capture adapter's ``healthy()``):
      * ``attached_page``    the page being captured, or None when detached;
      * ``last_message_at``  ``clock()`` (monotonic) of the last accepted message
        from that page (hello/rescan/live), or None before the first one;
      * ``counts``           accepted messages per kind (``hello``/``rescan``/``live``).
    """

    def __init__(self, context: "BrowserContext", *, clock: Callable[[], float] = time.monotonic) -> None:
        self._context = context
        self._clock = clock
        self._router = _router_for(context)
        self.last_message_at: float | None = None
        self.counts: Counter[str] = Counter()

    @property
    def attached_page(self) -> "Page | None":
        r = self._router
        return r.page if r.owner is self else None

    def _accept(self, kind: str) -> None:
        self.last_message_at = self._clock()
        self.counts[str(kind)] += 1

    async def attach(self, page: "Page", log: CommentLog) -> None:
        """Capture ``page`` into ``log`` (replacing any previously attached page,
        also one attached through another handle on the same context).

        The binding and init script are installed once per context. A page that
        is already loaded is reloaded so the watcher runs from document start;
        the rescan that follows fills any gap.
        """
        await self._router.ensure_installed()
        self._router.route(self, page, log)
        self.last_message_at = None
        if page.url not in ("", "about:blank"):
            await page.reload()

    async def detach(self) -> None:
        """Stop routing; later messages from the page are dropped.

        A superseded handle (another one attached since) does nothing.
        """
        if self._router.owner is self:
            self._router.route(self, None, None)
