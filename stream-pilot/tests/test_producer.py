"""Producer flows against the local fixture pages (inside the image's Chromium).

Playwright is imported lazily (inside the page fixture) so collection works on a
plain host, where every test here is skipped.
"""

from __future__ import annotations

import json
import logging
import stat
import time
from pathlib import Path

import pytest
import pytest_asyncio

from stream_pilot.fb import producer as producer_mod
from stream_pilot.fb.producer import Producer
from stream_pilot.ports import StepFailed, StreamTarget

pytestmark = pytest.mark.browser

GROUP = "Test Resale Group"
LIVE_ID = "1122334455667788"
STEP_TIMEOUT = 3.0


@pytest.fixture
def values(fixtures_dir: Path) -> dict:
    return json.loads((fixtures_dir / "values.json").read_text())


@pytest_asyncio.fixture
async def page(pw_browser):
    context = pw_browser.contexts[0] if pw_browser.contexts else await pw_browser.new_context()
    page = await context.new_page()
    yield page
    await page.close()


def make(page, **kw) -> Producer:
    kw.setdefault("group_name", GROUP)
    kw.setdefault("step_timeout_s", STEP_TIMEOUT)
    return Producer(page, **kw)


async def to_setup_page(page, base: str, query: str = "") -> Producer:
    """Producer sitting on the setup page (as after setup()), ready for the go-live tests."""
    producer = make(page)
    await producer.open(f"{base}/producer_setup.html{query}")
    await page.get_by_role("button", name="Streaming software").click()
    return producer


async def test_setup_returns_target_and_enables_persistent_key(page, fixture_base_url, values):
    producer = make(page)
    await producer.open(f"{fixture_base_url}/live/producer/")
    target = await producer.setup()
    assert target == StreamTarget(server=values["server_url"], key=values["stream_key"])
    switch = page.get_by_role("switch", name="Persistent stream key")
    assert await switch.get_attribute("aria-checked") == "true"
    # end-live-when-stream-stops was already on: left alone
    end = page.get_by_role("switch", name="End live video when stream stops")
    assert await end.get_attribute("aria-checked") == "true"


async def test_setup_turns_end_on_stop_on_when_off(page, fixture_base_url):
    producer = make(page)
    await producer.open(f"{fixture_base_url}/live/producer/?endoff=1")
    await producer.setup()
    end = page.get_by_role("switch", name="End live video when stream stops")
    assert await end.get_attribute("aria-checked") == "true"


async def test_end_on_stop_absent_is_not_an_error(page, fixture_base_url, values, caplog):
    producer = make(page)
    await producer.open(f"{fixture_base_url}/live/producer/?noendstop=1")
    with caplog.at_level(logging.INFO, logger="stream_pilot"):
        target = await producer.setup()
    assert target.key == values["stream_key"]
    assert any(r.levelno == logging.INFO and "end_on_stop" in r.getMessage() for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_setup_never_logs_the_key(page, fixture_base_url, values, caplog):
    producer = make(page)
    await producer.open(f"{fixture_base_url}/live/producer/")
    with caplog.at_level(logging.DEBUG):
        await producer.setup()
    assert values["stream_key"] not in caplog.text


async def test_missing_key_fails_read_key(page, fixture_base_url):
    producer = make(page)
    await producer.open(f"{fixture_base_url}/live/producer/?nokey=1")
    with pytest.raises(StepFailed) as exc:
        await producer.setup()
    assert exc.value.step == "read_key"


async def test_wrong_group_fails_group_step(page, fixture_base_url):
    producer = make(page, group_name="Some Other Group", step_timeout_s=1.0)
    await producer.open(f"{fixture_base_url}/live/producer/")
    with pytest.raises(StepFailed) as exc:
        await producer.setup()
    assert exc.value.step == "group"


async def test_group_name_is_matched_literally(page, fixture_base_url):
    # regex metacharacters in a configured group name must not act as a pattern
    producer = make(page, group_name="Test.*", step_timeout_s=1.0)
    await producer.open(f"{fixture_base_url}/live/producer/")
    with pytest.raises(StepFailed) as exc:
        await producer.setup()
    assert exc.value.step == "group"


async def test_missing_streaming_software_fails_that_step(page, fixture_base_url):
    producer = make(page, step_timeout_s=1.0)
    await producer.open(f"{fixture_base_url}/live/producer/?nosw=1")
    with pytest.raises(StepFailed) as exc:
        await producer.setup()
    assert exc.value.step == "streaming_software"


async def test_stuck_persistent_switch_fails_persistent_key_step(page, fixture_base_url):
    producer = make(page, step_timeout_s=2.0)
    await producer.open(f"{fixture_base_url}/live/producer/?stuckswitch=1")
    with pytest.raises(StepFailed) as exc:
        await producer.setup()
    assert exc.value.step == "persistent_key"
    assert "did not turn on" in exc.value.detail


async def test_advanced_already_open_is_not_clicked(page, fixture_base_url, values):
    producer = make(page)
    await producer.open(f"{fixture_base_url}/live/producer/?advopen=1")
    assert (await producer.setup()).key == values["stream_key"]
    adv = page.get_by_role("button", name="Advanced settings")
    assert await adv.get_attribute("aria-expanded") == "true"  # still open: it was never toggled


async def test_advanced_open_with_late_switch_is_not_collapsed(page, fixture_base_url, values):
    # the switch renders after the "already visible?" grace; clicking Advanced then would close the panel
    producer = make(page, step_timeout_s=5.0)
    await producer.open(f"{fixture_base_url}/live/producer/?advopen=1&late=1500")
    assert (await producer.setup()).key == values["stream_key"]
    adv = page.get_by_role("button", name="Advanced settings")
    assert await adv.get_attribute("aria-expanded") == "true"


async def test_wait_preview_ready_times_out_then_passes(page, fixture_base_url):
    producer = await to_setup_page(page, fixture_base_url)
    with pytest.raises(StepFailed) as exc:
        await producer.wait_preview_ready(0.8)
    assert exc.value.step == "preview"

    await page.evaluate("window.__previewReady()")
    started = time.monotonic()
    await producer.wait_preview_ready(5.0)
    assert time.monotonic() - started < 4.0


async def test_wait_preview_ready_ignores_the_step_tracker_go_live(page, fixture_base_url):
    # The step tracker's "Go live" is always enabled; only the real button means a preview.
    producer = await to_setup_page(page, fixture_base_url, "?stepper=1")
    with pytest.raises(StepFailed) as exc:
        await producer.wait_preview_ready(0.8)
    assert exc.value.step == "preview"
    await page.evaluate("window.__previewReady()")
    await producer.wait_preview_ready(5.0)


async def test_go_live_clicks_the_real_button_not_the_step_tracker(page, fixture_base_url):
    producer = await to_setup_page(page, fixture_base_url, "?stepper=1")
    await page.evaluate("window.__previewReady()")
    await producer.wait_preview_ready(5.0)
    assert await producer.go_live(10.0) == LIVE_ID
    assert await page.evaluate("sessionStorage.getItem('stepperClicked')") is None


async def test_preview_lost_reflects_page_text(page, fixture_base_url):
    producer = await to_setup_page(page, fixture_base_url)
    await page.evaluate("window.__previewReady()")
    await producer.wait_preview_ready(5.0)
    assert await producer.preview_lost() is False
    await page.evaluate(
        "document.body.insertAdjacentHTML('beforeend', '<p>Your stream is disconnected</p>')"
    )
    assert await producer.preview_lost() is True


async def test_preview_lost_when_go_live_turns_disabled_or_disappears(page, fixture_base_url):
    # Seen on the stream PC: Facebook went back to "Select a video source" with Go live
    # disabled and no "lost" text, while stream-pilot still showed GO LIVE.
    producer = await to_setup_page(page, fixture_base_url)
    await page.evaluate("window.__previewReady()")
    await producer.wait_preview_ready(5.0)
    await page.evaluate("document.getElementById('go').disabled = true")
    assert await producer.preview_lost() is True
    await page.evaluate("document.getElementById('go').remove()")
    assert await producer.preview_lost() is True


async def test_preview_lost_ignores_a_go_live_flicker(page, fixture_base_url):
    producer = await to_setup_page(page, fixture_base_url)
    await page.evaluate("window.__previewReady()")
    await producer.wait_preview_ready(5.0)
    await page.evaluate(
        "const g = document.getElementById('go'); g.disabled = true;"
        " setTimeout(() => { g.disabled = false; }, 300)"
    )
    assert await producer.preview_lost() is False


async def test_go_live_confirms_interstitial_and_returns_live_id(page, fixture_base_url):
    producer = await to_setup_page(page, fixture_base_url)
    await page.evaluate("window.__previewReady()")
    await producer.wait_preview_ready(5.0)
    assert await producer.go_live(10.0) == LIVE_ID
    assert LIVE_ID in page.url


async def test_go_live_fills_required_title(page, fixture_base_url, caplog):
    producer = await to_setup_page(page, fixture_base_url, "?title=1")
    await page.evaluate("window.__previewReady()")
    await producer.wait_preview_ready(5.0)
    with caplog.at_level(logging.INFO, logger="stream_pilot"):
        assert await producer.go_live(10.0) == LIVE_ID
    assert await page.evaluate("sessionStorage.getItem('typedTitle')") == "Live"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("filled required title field with default" in m for m in warnings), warnings


async def test_go_live_does_not_type_into_required_non_text_controls(page, fixture_base_url, caplog):
    producer = await to_setup_page(page, fixture_base_url, "?reqcheck=1")
    await page.evaluate("window.__previewReady()")
    await producer.wait_preview_ready(5.0)
    with caplog.at_level(logging.INFO, logger="stream_pilot"):
        assert await producer.go_live(10.0) == LIVE_ID
    assert any("skipping" in r.getMessage() and "required" in r.getMessage() for r in caplog.records)
    # nothing was typed anywhere
    assert await page.evaluate("sessionStorage.getItem('typedTitle')") is None


async def test_go_live_failure_detail_has_state_and_last_error(page, fixture_base_url, caplog):
    # the confirm button never becomes clickable: the detail names the state and the click error
    producer = await to_setup_page(page, fixture_base_url, "?disabledconfirm=1")
    await page.evaluate("window.__previewReady()")
    await producer.wait_preview_ready(5.0)
    with caplog.at_level(logging.WARNING, logger="stream_pilot"):
        with pytest.raises(StepFailed) as exc:
            await producer.go_live(3.0)
    assert exc.value.step == "go_live"
    assert "dialog_seen=True" in exc.value.detail
    assert "last error" in exc.value.detail
    # the same error is logged once, not on every poll
    polls = [r for r in caplog.records if r.levelno == logging.WARNING and "go_live poll" in r.getMessage()]
    assert len(polls) == 1, [r.getMessage() for r in polls]


async def test_go_live_does_not_reclick_a_dialog_that_ignores_the_click(page, fixture_base_url):
    producer = await to_setup_page(page, fixture_base_url, "?stuckdialog=1")
    await page.evaluate("window.__previewReady()")
    await producer.wait_preview_ready(5.0)
    with pytest.raises(StepFailed) as exc:
        await producer.go_live(2.0)
    assert exc.value.step == "go_live"
    assert "dialog_seen=True" in exc.value.detail
    assert "confirms=1" in exc.value.detail  # one click, then the dialog is given time to go away


async def test_go_live_fails_when_never_live(page, fixture_base_url):
    # Go live stays disabled (no __previewReady): the click cannot happen
    producer = await to_setup_page(page, fixture_base_url)
    with pytest.raises(StepFailed) as exc:
        await producer.go_live(1.5)
    assert exc.value.step == "go_live"


async def test_is_login_page(page, fixture_base_url):
    producer = make(page)
    await producer.open(f"{fixture_base_url}/login.html")
    assert await producer.is_login_page() is True
    await producer.open(f"{fixture_base_url}/live/producer/")
    assert await producer.is_login_page() is False


async def test_in_login_flow_while_the_login_form_is_shown(page, fixture_base_url):
    producer = make(page)
    await producer.open(f"{fixture_base_url}/login.html")
    assert await producer.in_login_flow() is True
    await producer.open(f"{fixture_base_url}/live/producer/")
    assert await producer.in_login_flow() is False


async def test_save_failure_writes_three_files(page, fixture_base_url, tmp_path):
    producer = make(page)
    await producer.open(f"{fixture_base_url}/live/producer/")
    out = tmp_path / "failures" / "read_key"
    await producer.save_failure("read_key", out)
    for name in ("screenshot.png", "page.html", "aria.yml"):
        assert (out / name).stat().st_size > 0, name
    assert "Go live" in (out / "aria.yml").read_text()


async def test_save_failure_never_raises(page, fixture_base_url, tmp_path):
    producer = make(page)
    await page.close()
    await producer.save_failure("x", tmp_path / "f")  # page is gone: nothing to save, no exception


async def test_record_dir_gets_screenshot_and_aria_per_step(page, fixture_base_url, tmp_path):
    rec = tmp_path / "rec"
    producer = make(page, record_dir=rec)
    await producer.open(f"{fixture_base_url}/live/producer/")
    await producer.setup()
    names = sorted(p.name for p in rec.iterdir())
    steps = ["group", "go_live_start", "streaming_software", "advanced", "persistent_key", "end_on_stop", "read_key"]
    expected = sorted(f"{i:02d}-{s}.{ext}" for i, s in enumerate(steps, 1) for ext in ("png", "yml"))
    assert names == expected
    assert (rec / "05-persistent_key.png").stat().st_size > 0
    assert "Persistent stream key" in (rec / "05-persistent_key.yml").read_text()


async def test_go_live_records_the_click_and_the_live_dashboard(page, fixture_base_url, tmp_path):
    rec = tmp_path / "rec"
    producer = make(page, record_dir=rec)
    await producer.open(f"{fixture_base_url}/producer_setup.html")
    await page.get_by_role("button", name="Streaming software").click()
    await page.evaluate("window.__previewReady()")
    await producer.wait_preview_ready(5.0)
    assert await producer.go_live(10.0) == LIVE_ID
    assert sorted(p.name for p in rec.iterdir()) == [
        "01-go_live_clicked.png", "01-go_live_clicked.yml", "02-go_live.png", "02-go_live.yml",
    ]


async def test_unreachable_open_raises_step_failed(page):
    producer = make(page, step_timeout_s=2.0)
    with pytest.raises(StepFailed) as exc:
        await producer.open("http://127.0.0.1:1/live/producer/")
    assert exc.value.step == "open"


async def test_files_are_utf8_and_private(page, fixture_base_url, tmp_path):
    await page.goto(f"{fixture_base_url}/live/producer/")
    await page.evaluate("document.body.insertAdjacentHTML('beforeend', '<p>Caf\u00e9 \u2603 gr\u00fcppe</p>')")
    producer = make(page, record_dir=tmp_path / "rec")
    out = tmp_path / "fail"
    out.mkdir(mode=0o755)  # pre-existing, too-open directory must be tightened
    await producer.save_failure("x", out)
    assert "Caf\u00e9 \u2603 gr\u00fcppe" in (out / "page.html").read_text(encoding="utf-8")
    await producer._record("probe")
    for directory in (out, tmp_path / "rec"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
        for f in directory.iterdir():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


async def test_blank_group_name_is_rejected(page):
    with pytest.raises(ValueError):
        make(page, group_name="  ")


# ---- bounded calls ------------------------------------------------------------


async def hang_renderer(page) -> None:
    """Freeze the page's main thread: every later locator/evaluate call on it blocks."""
    await page.evaluate("setTimeout(() => { while (true) {} }, 0)")
    await page.wait_for_timeout(200)


@pytest.fixture
def tight_bounds(monkeypatch):
    monkeypatch.setattr(producer_mod, "_GRACE_S", 0.3)
    monkeypatch.setattr(producer_mod, "_QUERY_TIMEOUT_S", 0.5)


async def test_go_live_is_bounded_when_the_page_hangs(page, fixture_base_url, tight_bounds):
    producer = await to_setup_page(page, fixture_base_url)
    await hang_renderer(page)
    started = time.monotonic()
    with pytest.raises(StepFailed) as exc:
        await producer.go_live(1.0)
    assert exc.value.step == "go_live"
    assert time.monotonic() - started < 4.0


async def test_wait_preview_ready_is_bounded_when_the_page_hangs(page, fixture_base_url, tight_bounds):
    producer = await to_setup_page(page, fixture_base_url)
    await hang_renderer(page)
    started = time.monotonic()
    with pytest.raises(StepFailed) as exc:
        await producer.wait_preview_ready(1.0)
    assert exc.value.step == "preview"
    assert time.monotonic() - started < 4.0


async def test_soft_end_on_stop_is_bounded_when_the_page_hangs(page, fixture_base_url, caplog):
    producer = make(page, step_timeout_s=1.0)
    await producer.open(f"{fixture_base_url}/producer_setup.html?endoff=1")
    await page.get_by_role("button", name="Streaming software").click()
    await hang_renderer(page)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        await producer._step_end_on_stop()
    assert time.monotonic() - started < 3.0
    assert "end_on_stop: could not check/enable" in caplog.text


async def test_queries_are_bounded_when_the_page_hangs(page, fixture_base_url, tight_bounds):
    producer = await to_setup_page(page, fixture_base_url)
    await hang_renderer(page)
    started = time.monotonic()
    with pytest.raises(StepFailed) as login:
        await producer.is_login_page()
    with pytest.raises(StepFailed) as lost:
        await producer.preview_lost()
    assert login.value.step == "login_check"
    assert lost.value.step == "preview_lost"
    assert time.monotonic() - started < 4.0


async def test_queries_raise_step_failed_on_a_closed_page(page, fixture_base_url):
    producer = make(page)
    await producer.open(f"{fixture_base_url}/live/producer/")
    await page.close()
    with pytest.raises(StepFailed) as login:
        await producer.is_login_page()
    assert login.value.step == "login_check"
    with pytest.raises(StepFailed) as lost:
        await producer.preview_lost()
    assert lost.value.step == "preview_lost"


# ---- post details: the live's title -------------------------------------------


async def test_set_title_fills_the_composer_and_saves(page, fixture_base_url):
    producer = await to_setup_page(page, fixture_base_url)
    await producer.set_title("Test Resale Group Sale – Nov 24")
    assert await page.evaluate("sessionStorage.getItem('postTitle')") == "Test Resale Group Sale – Nov 24"
    assert not await page.get_by_role("dialog").filter(visible=True).count()
    # once saved, the button reads "Edit Post Details": a second setup still finds it
    await producer.set_title("Second title")
    assert await page.evaluate("sessionStorage.getItem('postTitle')") == "Second title"


async def test_set_title_does_not_touch_the_stream_key_or_go_live(page, fixture_base_url):
    producer = await to_setup_page(page, fixture_base_url)
    await producer.set_title("x")
    assert await page.get_by_role("textbox", name="Stream key").count() == 1
    assert await page.get_by_role("button", name="Go live").last.is_disabled()


async def test_set_title_without_post_details_fails_as_a_step(page, fixture_base_url):
    producer = await to_setup_page(page, fixture_base_url, "?notitle=1")
    with pytest.raises(StepFailed) as ei:
        await producer.set_title("x")
    assert ei.value.step == "title"


async def test_a_failed_title_closes_the_composer(page, fixture_base_url):
    producer = await to_setup_page(page, fixture_base_url, "?nosave=1")
    with pytest.raises(StepFailed) as ei:
        await producer.set_title("x")
    assert ei.value.step == "title"
    assert not await page.locator("#composer").is_visible()
