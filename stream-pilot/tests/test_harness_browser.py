"""Harness tests: fixture-server path mapping (host) and a trivial CDP smoke test (browser)."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from browser_harness import fixture_server, map_fixture_path
from stream_pilot.fb import selectors

LIVE_ID = "1122334455667788"


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read().decode()


async def test_fixture_server_maps_facebook_shaped_paths(fixtures_dir):
    import asyncio

    async with fixture_server(fixtures_dir) as base:
        get = lambda p: asyncio.to_thread(_get, base + p)  # noqa: E731
        assert "Test Resale Group" in await get("/producer_start.html")
        assert "Test Resale Group" in await get("/live/producer/?actor_id=1&group_id=2")
        assert "End live video" in await get(f"/live/producer/dashboard/{LIVE_ID}/")
        assert "End live video" in await get(f"/live/producer/dashboard/{LIVE_ID}")
        assert "__addComment" in await get(f"/live/producer/dashboard/{LIVE_ID}/COMMENTS/")
        assert "__addComment" in await get(f"/live/producer/dashboard/{LIVE_ID}/COMMENTS/?x=1")
        with pytest.raises(urllib.error.HTTPError) as exc:
            await get("/nope.html")
        assert exc.value.code == 404


def test_map_fixture_path():
    assert map_fixture_path("/live/producer/") == "producer_start.html"
    assert map_fixture_path("/live/producer/dashboard/12345678/") == "producer_live.html"
    assert map_fixture_path("/live/producer/dashboard/12345678/COMMENTS/") == "comments_popout.html"
    assert map_fixture_path("/producer_setup.html") is None


def test_fixtures_are_self_contained(fixtures_dir):
    for page in fixtures_dir.glob("*.html"):
        assert "http://" not in page.read_text() and "https://" not in page.read_text(), page.name


@pytest.mark.browser
async def test_go_live_button_found_over_cdp(pw_browser, fixture_base_url):
    context = pw_browser.contexts[0] if pw_browser.contexts else await pw_browser.new_context()
    page = await context.new_page()
    await page.goto(f"{fixture_base_url}/producer_start.html")
    button = page.get_by_role("button", name=selectors.GO_LIVE)
    await button.wait_for(timeout=5000)
    assert await button.count() == 1


@pytest.mark.browser
async def test_setup_flow_reaches_live_page(pw_browser, fixture_base_url):
    context = pw_browser.contexts[0] if pw_browser.contexts else await pw_browser.new_context()
    page = await context.new_page()
    await page.goto(f"{fixture_base_url}/live/producer/?actor_id=1")
    await page.get_by_role("button", name=selectors.GO_LIVE).click()
    await page.get_by_role("button", name=selectors.STREAMING_SOFTWARE).click()
    await page.get_by_role("button", name=selectors.ADVANCED_SETTINGS).click()
    assert await page.get_by_role("switch", name=selectors.PERSISTENT_KEY).get_attribute("aria-checked") == "false"
    assert await page.get_by_role("switch", name=selectors.END_LIVE_WHEN_STREAM_STOPS).get_attribute("aria-checked") == "true"
    go = page.get_by_role("button", name=selectors.GO_LIVE)
    assert await go.is_disabled()
    await page.evaluate("window.__previewReady()")
    await page.wait_for_function("!document.getElementById('go').disabled", timeout=5000)
    await go.click()
    await page.get_by_role("dialog").get_by_role("button", name=selectors.INTERSTITIAL_CONFIRM).click()
    await page.wait_for_url(f"**/live/producer/dashboard/{LIVE_ID}/")
    await page.get_by_role("button", name=selectors.END_LIVE_VIDEO).wait_for(timeout=5000)


@pytest.mark.browser
async def test_comments_fixture_add_and_reload(pw_browser, fixture_base_url):
    context = pw_browser.contexts[0] if pw_browser.contexts else await pw_browser.new_context()
    page = await context.new_page()
    await page.goto(f"{fixture_base_url}/live/producer/dashboard/{LIVE_ID}/COMMENTS/")
    articles = page.locator(selectors.COMMENT_ARTICLE_SELECTOR)
    assert await articles.count() == 4
    label = await articles.first.get_attribute("aria-label")
    assert selectors.parse_comment_label(label) == "Ann"
    await page.evaluate("window.__addComment('Eve', 'hi', true)")
    assert await articles.count() == 5
    async with page.expect_navigation():
        await page.evaluate("window.__reloadWith(2)")
    assert await articles.count() == 2
    assert "Eve" in (await articles.last.get_attribute("aria-label"))
