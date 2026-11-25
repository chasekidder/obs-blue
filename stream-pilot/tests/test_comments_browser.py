"""DOM comment capture in headless Chromium (fixture page only, never facebook.com).

The fixture renders its initial list asynchronously (two chunks, like React),
so these tests exercise the watcher's settle window, rescan/live
classification and the Python-side routing end to end through a Playwright
binding.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
import pytest_asyncio

from stream_pilot.comment_log import CommentLog
from stream_pilot.fb import selectors as S
from stream_pilot.fb.comments import FbDomCapture

pytestmark = pytest.mark.browser

LIVE_ID = "1122334455667788"
SEED = [
    ("Ann", "Is the lamp still available?"),
    ("Bob", "sold?"),
    ("Cara", "How much for the chair?"),
    ("Dan", "Can I pick up tomorrow"),
]
SETTLE_GRACE = 1.5  # > debounce (0.25 s) and, after reloads, comfortably past the fixture's chunks


def rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in out if r.get("source") == "fb_dom"]


def pairs(path: Path) -> list[tuple[str, str]]:
    return [(r["author"], r["text"]) for r in rows(path)]


async def wait_until(pred, timeout: float = 8.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class Env:
    def __init__(self, context, page, log: CommentLog, cap: FbDomCapture, base: str):
        self.context, self.page, self.log, self.cap, self.base = context, page, log, cap, base

    @property
    def path(self) -> Path:
        return self.log.path

    async def wait_rescans(self, n: int) -> None:
        await wait_until(lambda: self.cap.counts["rescan"] >= n, what=f"{n} rescan(s)")

    async def ready(self) -> None:
        await self.page.wait_for_function("window.__fixtureReady === true")


@pytest_asyncio.fixture
async def env(pw_browser, fixture_base_url, tmp_path):
    context = await pw_browser.new_context()
    await context.add_init_script("window.__spForceWatch = true")
    page = await context.new_page()
    await page.goto(f"{fixture_base_url}/comments_popout.html")  # already loaded: attach must reload
    log = CommentLog(tmp_path / "comments.jsonl", LIVE_ID)
    cap = FbDomCapture(context)
    await cap.attach(page, log)
    e = Env(context, page, log, cap, fixture_base_url)
    await e.wait_rescans(1)
    await e.ready()
    yield e
    await cap.detach()
    await context.close()


async def test_initial_rescan_logs_every_fixture_comment_once(env):
    # the list renders in two chunks; the settle window must wait for both
    assert pairs(env.path) == SEED
    first = rows(env.path)[0]
    assert (first["fb_comment_id"], first["author"], first["text"]) == ("9988776655443300", "Ann", SEED[0][1])
    assert rows(env.path)[1]["fb_comment_id"] is None
    assert all(r["live_id"] == LIVE_ID for r in rows(env.path))
    await asyncio.sleep(SETTLE_GRACE)
    assert pairs(env.path) == SEED, "nothing may be appended after the settle window"
    assert env.cap.counts["hello"] >= 1
    assert env.cap.counts["live"] == 0


async def test_same_author_same_text_twice_logs_two_more(env):
    await env.page.evaluate("window.__addComment('Eve', 'sold', false)")
    await env.page.evaluate("window.__addComment('Eve', 'sold', false)")
    await wait_until(lambda: len(rows(env.path)) == 6, what="two repeat claims")
    assert pairs(env.path)[4:] == [("Eve", "sold"), ("Eve", "sold")]
    await asyncio.sleep(SETTLE_GRACE)
    assert len(rows(env.path)) == 6


async def test_burst_in_one_debounce_window_keeps_dom_order(env):
    await env.page.evaluate(
        "for (const t of ['one','two','three']) window.__addComment('Fay', t, false)"
    )
    await wait_until(lambda: len(rows(env.path)) == 7, what="burst")
    assert [t for _, t in pairs(env.path)[4:]] == ["one", "two", "three"]


async def test_reload_and_reload_with_fewer_comments_duplicates_nothing(env):
    await env.page.evaluate("window.__addComment('Eve', 'sold', false)")
    await env.page.evaluate("window.__addComment('Eve', 'sold', false)")
    await wait_until(lambda: len(rows(env.path)) == 6)
    before = env.cap.counts["rescan"]
    await env.page.reload()
    await env.wait_rescans(before + 1)
    assert len(rows(env.path)) == 6
    before = env.cap.counts["rescan"]
    await env.page.evaluate("window.__reloadWith(3)")
    await env.wait_rescans(before + 1)
    await asyncio.sleep(0.5)
    assert len(rows(env.path)) == 6
    assert pairs(env.path)[:4] == SEED


async def test_unchanged_reload_appends_nothing(env):
    for _ in range(2):
        before = env.cap.counts["rescan"]
        await env.page.reload()
        await env.wait_rescans(before + 1)
    await asyncio.sleep(0.5)
    assert pairs(env.path) == SEED


async def test_comment_with_id_rerendered_after_reload_is_not_duplicated(env):
    await env.page.evaluate("window.__addComment('Gus', 'mine!', true)")
    await wait_until(lambda: len(rows(env.path)) == 5)
    assert rows(env.path)[4]["fb_comment_id"] is not None
    before = env.cap.counts["rescan"]
    await env.page.reload()
    await env.wait_rescans(before + 1)
    await asyncio.sleep(0.5)
    assert len(rows(env.path)) == 5


async def test_two_id_comments_get_distinct_ids_and_are_both_logged(env):
    # fixture IDs exceed 2^53: they must stay distinct (BigInt arithmetic)
    await env.page.evaluate("window.__addComment('Gus', 'mine!', true)")
    await env.page.evaluate("window.__addComment('Hana', 'mine too!', true)")
    await wait_until(lambda: len(rows(env.path)) == 6, what="two ID'd comments")
    ids = [r["fb_comment_id"] for r in rows(env.path)[4:]]
    assert all(ids) and len(set(ids)) == 2
    assert [a for a, _ in pairs(env.path)[4:]] == ["Gus", "Hana"]


async def test_id_dedupe_alone_prevents_duplicates_when_alignment_cannot(env):
    await env.page.evaluate("window.__addComment('Gus', 'mine!', true)")
    await env.page.evaluate("window.__addComment('Eve', 'sold', false)")
    await wait_until(lambda: len(rows(env.path)) == 6)
    before = env.cap.counts["rescan"]
    # the reloaded page shows only the ID'd comments [Ann, Cara, Gus]: they do not align
    # with the log's tail [Dan, Gus, Eve], so only ID dedupe can keep them out
    await env.page.evaluate("window.__reloadOnlyWithIds()")
    await env.wait_rescans(before + 1)
    await asyncio.sleep(0.5)
    assert len(rows(env.path)) == 6
    # all of them carry known ids: they overlap the log by id, so no gap is noted
    assert not env.log.gaps


async def test_binding_survives_reload(env):
    before = env.cap.counts["rescan"]
    await env.page.reload()
    await env.wait_rescans(before + 1)
    await env.ready()
    await env.page.evaluate("window.__addComment('Hal', 'after reload', false)")
    await wait_until(lambda: ("Hal", "after reload") in pairs(env.path), what="live comment after reload")
    assert len(rows(env.path)) == 5


async def test_prepended_older_comments_do_not_duplicate_history(env):
    await env.page.evaluate("window.__prependOlder(3, false)")
    await env.wait_rescans(2)  # an insertion before reported items forces a full rescan
    await asyncio.sleep(0.5)
    assert pairs(env.path) == SEED
    # and live comments keep flowing afterwards
    await env.page.evaluate("window.__addComment('Ivy', 'sold', false)")
    await wait_until(lambda: len(rows(env.path)) == 5)
    assert pairs(env.path)[4] == ("Ivy", "sold")


async def test_whole_list_replaced_is_a_rescan_not_live_and_duplicates_nothing(env):
    # every node replaced by a clone, one with slightly different text: no previously
    # reported article is still connected, so a "live" batch would re-append history
    await env.page.evaluate("window.__replaceAll(1)")
    await env.wait_rescans(2)
    await asyncio.sleep(SETTLE_GRACE)
    assert pairs(env.path) == SEED
    assert env.cap.counts["live"] == 0
    # and live capture keeps working afterwards
    await env.page.evaluate("window.__addComment('Ivy', 'sold', false)")
    await wait_until(lambda: len(rows(env.path)) == 5)
    assert pairs(env.path)[4] == ("Ivy", "sold")


async def test_identical_whole_list_rerender_appends_nothing(env):
    await env.page.evaluate("window.__replaceAll(-1)")
    await asyncio.sleep(SETTLE_GRACE)
    assert pairs(env.path) == SEED


async def test_text_that_fills_in_late_is_logged_once_with_the_final_text(env):
    # body still empty at the first debounce window (250 ms), filled at ~350 ms
    await env.page.evaluate("window.__addSlowComment('Slow', 'final text', 350)")
    await wait_until(lambda: any(a == "Slow" for a, _ in pairs(env.path)), what="slow comment")
    await asyncio.sleep(SETTLE_GRACE)
    assert [p for p in pairs(env.path) if p[0] == "Slow"] == [("Slow", "final text")]
    assert len(rows(env.path)) == 5


async def test_comment_that_stays_empty_is_reported_after_the_extra_window(env):
    t0 = time.monotonic()
    await env.page.evaluate("window.__addComment('Stick', '', false)")  # e.g. a sticker
    await wait_until(lambda: len(rows(env.path)) == 5, what="empty-text comment")
    assert time.monotonic() - t0 >= 0.45, "deferred by one extra debounce window"
    await asyncio.sleep(SETTLE_GRACE)
    assert pairs(env.path)[4:] == [("Stick", "")]


async def test_rerendered_last_node_appends_nothing(env):
    await env.page.evaluate("window.__rerenderLast()")
    await asyncio.sleep(SETTLE_GRACE)
    assert pairs(env.path) == SEED
    assert env.cap.counts["live"] == 0


async def test_rerender_does_not_swallow_a_real_repeat_in_the_same_window(env):
    await env.page.evaluate("window.__addComment('Zed', 'sold', false)")
    await wait_until(lambda: len(rows(env.path)) == 5)
    await env.page.evaluate("window.__rerenderLast(); window.__addComment('Zed', 'sold', false)")
    await wait_until(lambda: len(rows(env.path)) == 6, what="real repeat")
    await asyncio.sleep(SETTLE_GRACE)
    assert pairs(env.path)[4:] == [("Zed", "sold"), ("Zed", "sold")]


async def test_second_capture_on_the_same_context_attaches_and_captures(env, tmp_path):
    cap2 = FbDomCapture(env.context)  # same context: must reuse the one binding/init script
    log2 = CommentLog(tmp_path / "second.jsonl", "2222222222222222")
    await cap2.attach(env.page, log2)  # loaded page: reloads, rescan fills log2
    await wait_until(lambda: cap2.counts["rescan"] >= 1, what="rescan via the second capture")
    await env.ready()
    assert pairs(log2.path) == SEED
    assert cap2.attached_page is env.page and env.cap.attached_page is None
    await env.page.evaluate("window.__addComment('Kay', 'second handle', false)")
    await wait_until(lambda: ("Kay", "second handle") in pairs(log2.path), what="live via the second capture")
    # the first handle's routing was replaced: its log gets nothing more
    assert pairs(env.path) == SEED
    # and attaching the first handle again takes the routing back
    before = env.cap.counts["rescan"]
    await env.cap.attach(env.page, env.log)
    await env.wait_rescans(before + 1)
    await env.ready()
    await env.page.evaluate("window.__addComment('Lou', 'first again', false)")
    await wait_until(lambda: ("Lou", "first again") in pairs(env.path), what="live via the first capture")
    assert ("Lou", "first again") not in pairs(log2.path)


async def test_detach_stops_routing(env):
    await env.cap.detach()
    assert env.cap.attached_page is None
    await env.page.evaluate("window.__addComment('Jo', 'ignored', false)")
    await asyncio.sleep(SETTLE_GRACE)
    assert pairs(env.path) == SEED


async def test_messages_from_a_stale_page_are_dropped(env, tmp_path):
    page2 = await env.context.new_page()
    await page2.goto(f"{env.base}/comments_popout.html")
    log2 = CommentLog(tmp_path / "second.jsonl", "2222222222222222")
    await env.cap.attach(page2, log2)  # replaces the first page
    assert env.cap.attached_page is page2
    await wait_until(lambda: len(rows(log2.path)) == 4, what="new page capture")
    # the old page is still open and still running its watcher
    await env.page.evaluate("window.__addComment('Old', 'stale', false)")
    await page2.wait_for_function("window.__fixtureReady === true")
    await page2.evaluate("window.__addComment('New', 'fresh', false)")
    await wait_until(lambda: ("New", "fresh") in pairs(log2.path))
    await asyncio.sleep(SETTLE_GRACE)
    assert ("Old", "stale") not in pairs(log2.path)
    assert ("Old", "stale") not in pairs(env.path)
    assert len(rows(env.path)) == 4


async def test_last_message_at_and_attached_page(env):
    assert env.cap.attached_page is env.page
    t = env.cap.last_message_at
    assert t is not None and time.monotonic() - t < 30


async def test_comments_marker_activates_without_force_flag(pw_browser, fixture_base_url, tmp_path):
    context = await pw_browser.new_context()  # no __spForceWatch
    try:
        page = await context.new_page()
        log = CommentLog(tmp_path / "c.jsonl", LIVE_ID)
        cap = FbDomCapture(context)
        await cap.attach(page, log)  # about:blank: nothing to reload
        marker = S.popout_url_fragment(LIVE_ID)
        await page.goto(f"{fixture_base_url}/live/producer/dashboard/{LIVE_ID}/COMMENTS/{marker}")
        await wait_until(lambda: len(rows(log.path)) == 4, what="rescan on the marked page")
        assert pairs(log.path) == SEED
    finally:
        await context.close()


async def test_comments_module_is_pinned_over_the_window(pw_browser, fixture_base_url, tmp_path):
    context = await pw_browser.new_context()
    try:
        page = await context.new_page()
        log = CommentLog(tmp_path / "c.jsonl", LIVE_ID)
        cap = FbDomCapture(context)
        await cap.attach(page, log)
        await page.goto(f"{fixture_base_url}/live/producer/dashboard/{LIVE_ID}/COMMENTS/{S.popout_url_fragment(LIVE_ID)}")
        await wait_until(lambda: len(rows(log.path)) == 4, what="rescan on the marked page")
        pinned = await page.evaluate(
            """() => { const m = document.querySelector('[data-sp-comments-focus]');
                       return m && [m.id, getComputedStyle(m).position]; }"""
        )
        assert pinned == ["module", "fixed"]
        # minimizing the module would collapse the list the watcher reads
        assert await page.evaluate("getComputedStyle(document.getElementById('minimize')).display") == "none"
    finally:
        await context.close()


async def test_other_pages_are_ignored_without_force_flag(pw_browser, fixture_base_url, tmp_path):
    context = await pw_browser.new_context()
    try:
        page = await context.new_page()
        log = CommentLog(tmp_path / "c.jsonl", LIVE_ID)
        cap = FbDomCapture(context)
        await cap.attach(page, log)
        # the same page without stream-pilot's marker (e.g. the main producer tab)
        await page.goto(f"{fixture_base_url}/live/producer/dashboard/{LIVE_ID}/COMMENTS/")
        await asyncio.sleep(SETTLE_GRACE + 1.5)
        assert sum(cap.counts.values()) == 0
        assert rows(log.path) == []
    finally:
        await context.close()


async def test_empty_page_reports_alive_then_late_comments_arrive_as_rescan(pw_browser, fixture_base_url, tmp_path):
    context = await pw_browser.new_context()
    await context.add_init_script("window.__spForceWatch = true")
    try:
        page = await context.new_page()
        log = CommentLog(tmp_path / "c.jsonl", LIVE_ID)
        cap = FbDomCapture(context)
        await cap.attach(page, log)
        await page.goto(f"{fixture_base_url}/producer_start.html")  # forced, but has no articles
        await wait_until(lambda: cap.counts["rescan"] == 1, timeout=15, what="empty rescan after 10 s")
        assert rows(log.path) == []
        await page.evaluate(
            """() => { for (const [a, t] of [['Kim','late one'],['Lee','late two']]) {
                 const el = document.createElement('div');
                 el.setAttribute('role', 'article');
                 el.setAttribute('aria-label', 'Comment by ' + a + ' just now');
                 el.textContent = a + ' ' + t;
                 document.body.append(el);
            } }"""
        )
        await wait_until(lambda: cap.counts["rescan"] == 2, timeout=8, what="late rescan")
        assert cap.counts["live"] == 0
        assert [a for a, _ in pairs(log.path)] == ["Kim", "Lee"]
        assert [t for _, t in pairs(log.path)] == ["late one", "late two"]
    finally:
        await context.close()


# ---- [claim_highlight] ---------------------------------------------------------

from stream_pilot.claims import CLAIM_CLASS  # noqa: E402

# Invented buyers; texts in the shapes seen in real sales.
CLAIM_COMMENTS = [
    ("Rita Vale", "21 cookie sheets 3", True),
    ("Omar Lind", "26blue 7", True),
    ("Pia Stone", "$5 crew mens#31", True),
    ("Lev Moss", "#31$house$3", True),
    ("Ida Crane", "18 $6 x mas wreath", True),
    ("Tom Reyes", "55 4 teal", True),
    ("Ana Holt", "6 resess X2 $3", True),
    ("Gil Park", "45 penguin gift tags 2/1.00", True),
    ("Uma Ford", "10- hat 3.00", True),
    ("Tom Reyes", "55 how much", False),
    ("Ana Holt", "6 sure", False),
    ("Joe Wren", "20 goodnight everyone", False),
    ("Kim Ash", "Hi Penny", False),
]


async def highlight_env(pw_browser, fixture_base_url, tmp_path, *, on: bool):
    context = await pw_browser.new_context()
    await context.add_init_script("window.__spForceWatch = true")
    page = await context.new_page()
    await page.goto(f"{fixture_base_url}/comments_popout.html")
    log = CommentLog(tmp_path / "comments.jsonl", LIVE_ID)
    cap = FbDomCapture(context, claim_highlight=on)
    await cap.attach(page, log)
    e = Env(context, page, log, cap, fixture_base_url)
    await e.wait_rescans(1)
    await e.ready()
    for author, text, _ in CLAIM_COMMENTS:
        await page.evaluate("([a, t]) => window.__addComment(a, t, true)", [author, text])
    await wait_until(lambda: len(rows(e.path)) == len(SEED) + len(CLAIM_COMMENTS), what="all comments")
    await asyncio.sleep(SETTLE_GRACE)
    return e


async def highlighted(page) -> dict[str, bool]:
    return await page.evaluate(
        """(cls) => Object.fromEntries(Array.from(document.querySelectorAll('[role=article]'))
             .map((a) => [a.querySelector('.text').textContent, a.classList.contains(cls)]))""",
        CLAIM_CLASS,
    )


async def test_claims_are_highlighted_and_capture_is_unchanged(pw_browser, fixture_base_url, tmp_path):
    e = await highlight_env(pw_browser, fixture_base_url, tmp_path, on=True)
    try:
        marks = await highlighted(e.page)
        for _, text, claim in CLAIM_COMMENTS:
            assert marks[text] is claim, text
        assert not any(marks[text] for _, text in SEED)
        # capture: exactly what was posted, once each, in order
        assert pairs(e.path) == SEED + [(a, t) for a, t, _ in CLAIM_COMMENTS]
        # the column is still pinned and "Minimize comments" still hidden
        assert await e.page.locator("[data-sp-comments-focus]").count() == 1
        assert not await e.page.locator("#minimize").is_visible()
        bg = await e.page.evaluate(
            "(c) => getComputedStyle(document.querySelector('.' + c)).backgroundColor", CLAIM_CLASS)
        assert bg != "rgba(0, 0, 0, 0)"
    finally:
        await e.cap.detach()
        await e.context.close()


async def test_claims_survive_a_react_rerender(pw_browser, fixture_base_url, tmp_path):
    e = await highlight_env(pw_browser, fixture_base_url, tmp_path, on=True)
    try:
        await e.page.evaluate("window.__replaceAll(-1)")
        await asyncio.sleep(SETTLE_GRACE)
        marks = await highlighted(e.page)
        assert all(marks[text] is claim for _, text, claim in CLAIM_COMMENTS)
    finally:
        await e.cap.detach()
        await e.context.close()


async def test_highlighting_off_adds_nothing(pw_browser, fixture_base_url, tmp_path):
    e = await highlight_env(pw_browser, fixture_base_url, tmp_path, on=False)
    try:
        assert not any((await highlighted(e.page)).values())
        assert await e.page.locator("#sp-claim-style").count() == 0
        assert pairs(e.path) == SEED + [(a, t) for a, t, _ in CLAIM_COMMENTS]
    finally:
        await e.cap.detach()
        await e.context.close()


async def test_pinned_comments_fill_the_window_width(env):
    await env.page.set_viewport_size({"width": 576, "height": 900})
    await env.page.evaluate("window.__addComment('Rita Vale', '21 cookie sheets 3', false)")
    await asyncio.sleep(SETTLE_GRACE)
    widths = await env.page.evaluate("""() => {
        const m = document.querySelector('[data-sp-comments-focus]').getBoundingClientRect().width;
        return Array.from(document.querySelectorAll('[role=article]'))
          .map((a) => a.getBoundingClientRect().width / m); }""")
    assert widths and min(widths) > 0.9, widths
    overflow = await env.page.evaluate(
        "() => getComputedStyle(document.querySelector('[data-sp-comments-focus]')).overflowX")
    assert overflow == "hidden"
