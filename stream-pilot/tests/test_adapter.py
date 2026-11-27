"""FacebookAdapter / CaptureAdapter / CdpBrowser against the fixture pages (inside the image).

The whole flow runs through the same path production uses: ``connect_over_cdp``
to a throwaway headless Chromium, with the Facebook URL templates pointed at
the local fixture server. Playwright is only imported inside ``CdpBrowser``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

from browser_harness import chromium_bin
from stream_pilot import browser as browser_mod
from stream_pilot.browser import CdpBrowser
from stream_pilot.config import load_config
from stream_pilot.fb.adapter import CaptureAdapter, FacebookAdapter, comments_url
from stream_pilot.ports import StreamTarget

pytestmark = pytest.mark.browser

GROUP = "Test Resale Group"
LIVE_ID = "1122334455667788"
SEED = [
    ("Ann", "Is the lamp still available?"),
    ("Bob", "sold?"),
    ("Cara", "How much for the chair?"),
    ("Dan", "Can I pick up tomorrow"),
]


async def wait_until(pred, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def dom_pairs(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [(r["author"], r["text"]) for r in rows if r.get("source") == "fb_dom"]


async def _noop_spawn(argv):
    """Handoff that fails at once (exit 21, like headless Chromium refusing it): the CDP fallback runs."""
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(21)
    return fut


def make_cfg(tmp_path: Path, base: str, cdp_url, *, producer_path: str = "/live/producer/"):
    site = tmp_path / "site.toml"
    site.write_text(
        f"""
[facebook]
actor_id = "111"
group_id = "222"
group_name = "{GROUP}"
producer_url_template = "{base}{producer_path}?actor={{actor_id}}&target_id={{group_id}}"
comments_url_template = "{base}/live/producer/dashboard/{{live_id}}/COMMENTS/"

[cdp]
url = "{cdp_url}"
user_data_dir = "{cdp_url.profile_dir}"
chromium_bin = "{chromium_bin()}"

[ssn]
enabled = false
session_id = ""

[timeouts]
step_s = 5
"""
    )
    return load_config([site])


@pytest.fixture
def values(fixtures_dir: Path) -> dict:
    return json.loads((fixtures_dir / "values.json").read_text())


@pytest_asyncio.fixture
async def stack(cdp_url, fixture_base_url, tmp_path):
    cfg = make_cfg(tmp_path, fixture_base_url, cdp_url)
    browser = CdpBrowser(cfg.cdp, spawn=_noop_spawn)
    browser.fallback_wait_s = 5.0
    fb = FacebookAdapter(cfg, browser)
    cap = CaptureAdapter(cfg, browser)
    yield cfg, browser, fb, cap
    await cap.stop()
    await fb.close_browser()


async def test_full_flow_against_fixtures(stack, values, tmp_path, caplog):
    cfg, browser, fb, cap = stack

    await fb.connect()
    assert browser.is_connected
    assert await fb.logged_in() is True

    target = await fb.setup_live()
    assert target == StreamTarget(server=values["server_url"], key=values["stream_key"])

    dashboard = await browser.dashboard_page()
    await dashboard.evaluate("window.__previewReady()")
    await fb.wait_preview_ready(5.0)
    assert await fb.preview_lost() is False
    assert await fb.go_live(10.0) == values["live_id"]

    with caplog.at_level(logging.WARNING, logger="stream_pilot.browser"):
        await fb.open_comments(LIVE_ID)
    popout = await browser.find_page(contains=f"/{LIVE_ID}/COMMENTS")
    assert popout is not None and popout is not dashboard
    assert popout in browser.popout_pages
    assert await browser.dashboard_page() is dashboard  # the popout is never mistaken for it

    stream_dir = tmp_path / "streams" / "s1"
    assert cap.healthy() is False
    await cap.start(LIVE_ID, stream_dir)
    assert cap.healthy() is True
    path = stream_dir / "comments.jsonl"
    await wait_until(lambda: len(dom_pairs(path)) >= len(SEED), what="the seed comments")
    assert dom_pairs(path) == SEED

    await fb.park_dashboard()
    assert dashboard.url.startswith(browser_mod.PARK_URL)  # parked, with a note for the host

    await cap.stop()
    assert cap.healthy() is False
    assert dom_pairs(path) == SEED  # the file is kept
    assert popout.is_closed()  # the popout window is closed and untracked on stop
    assert browser.popout_pages == []
    assert not dashboard.is_closed()

    await fb.close_browser()
    assert not browser.is_connected


async def test_logged_in_false_on_the_login_page(cdp_url, fixture_base_url, tmp_path):
    cfg = make_cfg(tmp_path, fixture_base_url, cdp_url, producer_path="/login.html")
    browser = CdpBrowser(cfg.cdp, spawn=_noop_spawn)
    fb = FacebookAdapter(cfg, browser)
    try:
        await fb.connect()
        assert await fb.logged_in() is False
    finally:
        await fb.close_browser()


async def test_facebook_adapter_reconnects_after_the_cdp_connection_dropped(stack):
    cfg, browser, fb, cap = stack
    await fb.connect()
    assert await fb.logged_in() is True
    await browser._browser.close()  # drops the CDP connection (Chromium itself keeps running)
    assert not browser.is_connected
    assert await fb.logged_in() is True  # reconnects by itself
    assert browser.is_connected
    assert await fb.preview_lost() is False


async def test_connect_is_idempotent(stack):
    cfg, browser, fb, cap = stack
    await fb.connect()
    ctx = browser.context
    await fb.connect()
    assert browser.context is ctx


async def test_capture_reattaches_after_popout_closed_without_duplicates(stack, tmp_path):
    cfg, browser, fb, cap = stack
    await fb.connect()
    await fb.open_comments(LIVE_ID)
    stream_dir = tmp_path / "s2"
    await cap.start(LIVE_ID, stream_dir)
    path = stream_dir / "comments.jsonl"
    await wait_until(lambda: len(dom_pairs(path)) >= len(SEED), what="the seed comments")
    first_page = cap.dom_capture.attached_page
    assert cap.healthy() is True

    await first_page.close()  # Chromium crash / popout closed
    assert cap.healthy() is False

    rescans = cap.dom_capture.counts["rescan"]
    await cap.start(LIVE_ID, stream_dir)  # what the controller does on the next Tick
    assert cap.healthy() is True
    assert cap.dom_capture.attached_page is not first_page
    await wait_until(lambda: cap.dom_capture.counts["rescan"] > rescans, what="the rescan of the new popout")
    await asyncio.sleep(0.5)
    assert dom_pairs(path) == SEED  # deduped: no duplicate lines


async def test_capture_start_opens_the_popout_when_nobody_did(stack, tmp_path):
    cfg, browser, fb, cap = stack
    await fb.connect()
    stream_dir = tmp_path / "s3"
    await cap.start(LIVE_ID, stream_dir)  # open_comments never ran
    assert cap.healthy() is True
    await wait_until(lambda: len(dom_pairs(stream_dir / "comments.jsonl")) >= len(SEED), what="comments")


async def test_open_app_window_fallback_uses_cdp_and_warns(stack, caplog):
    cfg, browser, fb, cap = stack
    await fb.connect()
    url = comments_url(cfg, LIVE_ID)
    with caplog.at_level(logging.WARNING, logger="stream_pilot.browser"):
        page = await browser.open_app_window(url, timeout_s=0.3)
    assert page.url.startswith(url)
    assert "falling back" in caplog.text.lower()
    assert await browser.open_app_window(url) is page  # already open: returned as is


@pytest.mark.parametrize("headless_flag", [False, True])
async def test_open_app_window_real_handoff_finding(cdp_url, fixture_base_url, tmp_path, caplog, headless_flag):
    """Try the real ``chromium --user-data-dir=<profile> --app=<url>`` handoff.

    Whatever headless Chromium does, the page must end up open (handoff or
    fallback); the outcome is printed (``-s``) and recorded in the task report.
    """
    cfg = make_cfg(tmp_path, fixture_base_url, cdp_url)
    extra = ["--no-sandbox"] if os.geteuid() == 0 else []
    if headless_flag:
        extra.append("--headless=new")

    async def spawn(argv):
        return await browser_mod.spawn_detached([argv[0], *extra, *argv[1:]], env=cdp_url.env)

    browser = CdpBrowser(cfg.cdp, spawn=spawn)
    fb = FacebookAdapter(cfg, browser)
    try:
        await fb.connect()
        url = comments_url(cfg, LIVE_ID)
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="stream_pilot.browser"):
            page = await browser.open_app_window(url, timeout_s=8)
        handed_off = "falling back" not in caplog.text.lower()
        print(
            f"\nHANDOFF-FINDING headless_flag={headless_flag} handed_off={handed_off} "
            f"after={time.monotonic() - started:.1f}s",
            file=sys.stderr,
        )
        assert page.url.startswith(url)
    finally:
        await fb.close_browser()


# ------------------------------------------------- stale comments column check

SEED_IDS = {"9988776655443300", "9988776655443302"}  # Ann and Cara carry permalinks


@pytest.fixture
def quick_fresh(monkeypatch):
    from stream_pilot.fb import adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "FRESH_SETTLE_S", 0.2)
    monkeypatch.setattr(adapter_mod, "FRESH_LOAD_S", 5.0)


async def _pages_now(browser) -> int:
    return len([p for p in browser.context.pages if not p.is_closed()])


async def test_read_in_background_returns_and_closes_the_tab(stack):
    cfg, browser, fb, cap = stack
    await fb.connect()
    dashboard = await browser.dashboard_page()
    before = await _pages_now(browser)
    url = cfg.facebook.comments_url_template.format(live_id=LIVE_ID)
    n = await browser.read_in_background(
        url, "document.querySelectorAll('[role=article]').length",
        ready_selector="[role=article]", timeout_s=5,
    )
    assert n == len(SEED)
    assert await _pages_now(browser) == before
    assert await browser.dashboard_page() is dashboard


async def test_check_fresh_reloads_a_stale_column(stack, tmp_path, quick_fresh, caplog):
    cfg, browser, fb, cap = stack
    await fb.connect()
    stream_dir = tmp_path / "fresh1"
    await cap.start(LIVE_ID, stream_dir)
    col = cap.dom_capture.attached_page
    # the column shows nothing (stale), while a fresh load shows the seed comments
    hellos = cap.dom_capture.counts["hello"]
    await col.evaluate("window.__reloadWith(0)")
    await wait_until(lambda: cap.dom_capture.counts["hello"] > hellos, what="the empty column's watcher")
    hellos = cap.dom_capture.counts["hello"]
    with caplog.at_level(logging.INFO, logger="stream_pilot.fb.adapter"):
        assert await cap.check_fresh(LIVE_ID) is True
    assert "missed 2 of 2 comment(s)" in caplog.text
    await wait_until(lambda: cap.dom_capture.counts["hello"] > hellos, what="the reloaded column's watcher")
    assert cap.dom_capture.attached_page is col  # the same column, reloaded
    assert cap.healthy() is True


async def test_check_fresh_leaves_an_up_to_date_column(stack, tmp_path, quick_fresh, caplog):
    cfg, browser, fb, cap = stack
    await fb.connect()
    stream_dir = tmp_path / "fresh2"
    await cap.start(LIVE_ID, stream_dir)
    await wait_until(lambda: len(dom_pairs(stream_dir / "comments.jsonl")) >= len(SEED), what="the seed")
    cap._log.last_dom_at -= 1000  # quiet for long enough to be checked
    rescans = cap.dom_capture.counts["rescan"]
    with caplog.at_level(logging.INFO, logger="stream_pilot.fb.adapter"):
        assert await cap.check_fresh(LIVE_ID) is False
    assert "up to date (2 comment(s) on Facebook)" in caplog.text
    await asyncio.sleep(0.5)
    assert cap.dom_capture.counts["rescan"] == rescans


async def test_check_fresh_skips_a_column_that_is_capturing(stack, tmp_path, quick_fresh, monkeypatch):
    cfg, browser, fb, cap = stack
    await fb.connect()
    stream_dir = tmp_path / "fresh3"
    await cap.start(LIVE_ID, stream_dir)
    await wait_until(lambda: len(dom_pairs(stream_dir / "comments.jsonl")) >= len(SEED), what="the seed")

    async def no_read(*a, **kw):
        raise AssertionError("a recently captured column must not be read again")

    monkeypatch.setattr(browser, "read_in_background", no_read)
    assert await cap.check_fresh(LIVE_ID) is False
    assert await cap.check_fresh("another live") is False


async def test_live_ended_reads_the_column(stack, tmp_path):
    cfg, browser, fb, cap = stack
    await fb.connect()
    await cap.start(LIVE_ID, tmp_path / "ended")
    col = cap.dom_capture.attached_page
    assert await cap.live_ended(LIVE_ID) is False
    await col.evaluate("""() => { const b = document.createElement('div');
        b.setAttribute('role', 'button'); b.textContent = 'Live video ended';
        document.body.append(b); }""")
    assert await cap.live_ended(LIVE_ID) is True
    assert await cap.live_ended("another live") is False
