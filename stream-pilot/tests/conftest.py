"""Shared pytest wiring: the ``browser`` marker and browser fixtures.

Browser tests run only with ``STREAM_PILOT_BROWSER_TESTS=1`` (inside the image,
where Playwright and Chromium exist); otherwise they are skipped. Playwright
is imported lazily inside fixtures so collection works on a plain host.

Fixtures (all function-scoped):
    fixtures_dir()                 -> Path   tests/fixtures/fb
    fixture_base_url   (async)     -> str    http://127.0.0.1:<port> serving fixtures_dir
    cdp_url            (async)     -> str    CDP http URL of a throwaway headless Chromium
    pw_browser         (async)     -> playwright Browser, attached via connect_over_cdp(cdp_url)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio

from browser_harness import chromium_bin, fixture_server, headless_chromium

_FIXTURES = Path(__file__).parent / "fixtures" / "fb"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "browser: needs headless Chromium + Playwright (set STREAM_PILOT_BROWSER_TESTS=1)"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("STREAM_PILOT_BROWSER_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="browser tests need STREAM_PILOT_BROWSER_TESTS=1")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def fixtures_dir() -> Path:
    return _FIXTURES


@pytest_asyncio.fixture
async def fixture_base_url(fixtures_dir: Path):
    async with fixture_server(fixtures_dir) as base_url:
        yield base_url


@pytest_asyncio.fixture
async def cdp_url():
    async with headless_chromium(chromium_bin()) as url:
        yield url


@pytest_asyncio.fixture
async def pw_browser(cdp_url: str):
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        try:
            yield browser
        finally:
            await browser.close()
    finally:
        await pw.stop()
