# Facebook Automation and Comment Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the real `FacebookPort` and `CommentCapturePort` for stream-pilot:
- attach to Chromium over CDP,
- drive the Live Producer by role/text,
- read the stream server and key,
- go live, open the comments popout as the chat-column app window, and park the dashboard,
- capture comments from the popout DOM and from SSN's chat-listener websocket into `comments.jsonl`.

Everything must be tested against local fixture pages in the built image's Chromium. Live validation against a private test Group is a runbook for the user, because the test Group doesn't exist yet.

**Architecture:**
- `browser.py` owns the Playwright CDP connection and window management.
- `fb/selectors.py` holds every piece of Facebook knowledge.
- `fb/producer.py` implements the flows on a Playwright `Page`.
- `fb/watcher.js` is injected with `add_init_script` and reports through `expose_binding`, so it survives popout reloads.
- `ssn_listener.py` is an independent websocket client.
- `fb/adapter.py` composes these into `FacebookPort`/`CommentCapturePort` and is wired into `app.build_app`.

**Tech Stack:** Playwright for Python (async, `connect_over_cdp`), the system Chromium in the image, `websockets`, pytest-asyncio. Browser tests run **inside the built image** via podman.

**Spec:** `docs/superpowers/specs/2026-11-09-stream-pilot-design.md`. **Depends on:** plans 2 and 1.

## Global Constraints

- **Host safety:** same as plans 1 and 2. Additionally, never open facebook.com or socialstream.ninja from automated tests. All tests use local fixture HTML and a local websocket server.
- No Facebook IDs, session IDs, cookies, or keys in the repo. Fixtures use fake values that look like real ones, e.g. key `FB-1234567890123456-0-AbCdEfGhIjKlMnOpQrStUvWx`, server `rtmps://edgetee-upload-test.xx.fbcdn.net:443/rtmp/`, live ID `1122334455667788`.
- `fb/selectors.py` is the **only** module allowed to contain Facebook page text, roles, CSS, or URL patterns.
- Matching uses Playwright role/text locators with case-insensitive regexes. Never use pixel coordinates or obfuscated class names.
- Browser tests are marked `@pytest.mark.browser`. They are skipped on the host unless `STREAM_PILOT_BROWSER_TESTS=1`. Run them with:

```bash
podman run --rm -v "$PWD/stream-pilot":/src:Z -w /src -e STREAM_PILOT_BROWSER_TESTS=1 localhost/obs-blue:dev \
  bash -c '/usr/lib/stream-pilot/venv/bin/pip install -q pytest pytest-asyncio && /usr/lib/stream-pilot/venv/bin/python -m pytest -q -m browser'
```

  The tests launch their own headless Chromium (`--headless=new`, `--no-sandbox` when root) with a temp profile and a free debugging port, then attach with `connect_over_cdp`, the same path production uses.
- The SSN extension needs a small SSN tab kept open in the main browser window (the MV3 helper page). `browser.py` keeps the extension's popup/menu page open in a background tab once the extension is installed. The URL is `chrome-extension://cppibjhfemifednoimlblfcmjgfhfjeg/popup.html`; verify the page name from the extension's `manifest.json` in the profile's `Extensions/` dir at runtime, and fall back to not opening it if absent.

---

## File Structure

```
stream-pilot/src/stream_pilot/
  browser.py            # CdpBrowser: connect, pages, app window, close
  fb/__init__.py
  fb/selectors.py       # all Facebook knowledge
  fb/producer.py        # flows on a Page
  fb/watcher.js         # MutationObserver comment watcher
  fb/comments.py        # FbDomCapture
  fb/adapter.py         # FacebookAdapter(FacebookPort), CaptureAdapter(CommentCapturePort)
  ssn_listener.py       # SsnListener
stream-pilot/tests/
  fixtures/fb/login.html producer_start.html producer_setup.html producer_ready.html
             producer_live.html comments_popout.html
  browser_harness.py    # launch headless chromium + local http server for fixtures
  test_selectors.py test_producer.py test_comments_browser.py test_ssn_listener.py test_adapter.py
docs/superpowers/runbooks/facebook-live-validation.md
```

---

### Task 1: Selectors and pure parsing

**Files:** Create `fb/__init__.py`, `fb/selectors.py`, `tests/test_selectors.py`

**Interfaces (exact names):**

```python
import re
GO_LIVE = re.compile(r"^\s*go live\s*$", re.I)
STREAMING_SOFTWARE = re.compile(r"streaming software", re.I)
ADVANCED_SETTINGS = re.compile(r"advanced settings", re.I)
PERSISTENT_KEY = re.compile(r"persistent stream key", re.I)
END_LIVE_WHEN_STREAM_STOPS = re.compile(r"end live video.*(stream|encoder).*stop", re.I)
END_LIVE_VIDEO = re.compile(r"^\s*end live video\s*$", re.I)
INTERSTITIAL_CONFIRM = re.compile(r"^\s*(go live|go live now|continue|skip)\s*$", re.I)
LOGIN_FORM_SELECTOR = "input[name='email'], input[name='pass'], form[action*='login']"
STREAM_KEY_RE = re.compile(r"^FB-[A-Za-z0-9_-]{16,}$")
SERVER_URL_RE = re.compile(r"^rtmps://[a-z0-9.-]+\.fbcdn\.net(:443)?/rtmp/?$", re.I)
LIVE_ID_IN_URL_RE = re.compile(r"/live/producer/(?:dashboard/)?(\d{6,})")
COMMENT_ID_IN_HREF_RE = re.compile(r"[?&]comment_id=(\d+)")
COMMENT_ARTICLE_SELECTOR = "[role='article']"
COMMENT_LABEL_RE = re.compile(r"^Comment by (?P<author>.+?)(?: (?:\d+ \w+ ago|a minute ago|just now|now))?$", re.I)
PREVIEW_LOST_TEXT = re.compile(r"(stream (is )?(disconnected|offline|not (yet )?(detected|connected)))|(lost connection)", re.I)

def pick_stream_key(values: list[str]) -> str | None: ...   # first value matching STREAM_KEY_RE (stripped)
def pick_server_url(values: list[str]) -> str | None: ...   # first matching SERVER_URL_RE; normalize to end with "/rtmp/"
def live_id_from_url(url: str) -> str | None: ...
```

- [ ] Steps:
  1. Failing tests: key and server picking from mixed lists (including masked `••••` values that must be ignored), server normalization, live ID extraction from both producer URL shapes, comment-label parsing, and the case-insensitive regexes.
  2. FAIL.
  3. Implement.
  4. PASS.
  5. Commit `feat(fb): selectors and parsing`.

---

### Task 2: Browser harness and fixtures

**Files:** Create `tests/browser_harness.py`, `tests/fixtures/fb/*.html`, `tests/conftest.py` additions (the `browser` marker and skip logic)

- `browser_harness.py`:
  - `async with fixture_server(dir) as base_url:` serves fixtures over `http.server` on 127.0.0.1 with a free port, in a thread.
  - `async with headless_chromium(bin) as cdp_url:` launches Chromium with a temp profile and a free debugging port, waits for `/json/version`, and kills Chromium on exit.
  - `chromium_bin()` reads `STREAM_PILOT_CHROMIUM` or the config default.
- **Fixtures.** Hand-written, minimal, accessible HTML that mimics the *roles and texts* the selectors expect, with small inline JS for state changes. They are not copies of Facebook.
  - `login.html`: has `input[name=email]` and `input[name=pass]`.
  - `producer_start.html`: shows the group name `Test Resale Group` and a `<button>Go live</button>` that navigates to `producer_setup.html`.
  - `producer_setup.html`:
    - radio/button "Streaming software" (clicking it reveals the key section),
    - an "Advanced settings" expander,
    - a `role=switch` with `aria-checked=false` named "Persistent stream key",
    - a `role=switch` "End live video when stream stops" with `aria-checked=true`,
    - read-only inputs holding the server URL and the key,
    - a disabled `Go live` button (`aria-disabled=true`) that becomes enabled 1 s after `window.__previewReady()` is called.
  - `producer_live.html`: URL path `/live/producer/dashboard/1122334455667788/` (serve a nested path), with an `End live video` button.
  - `producer_setup.html`'s enabled Go live opens an interstitial dialog with a `Go live` confirm. The confirm navigates to the live page.
  - `comments_popout.html`:
    - a list of `role=article` nodes with `aria-label="Comment by Ann 1 minute ago"`, an author link, a text div, and a permalink `?comment_id=NNN` on half of them (the other half have no ID),
    - `window.__addComment(author, text, withId)` appends a new article,
    - `window.__reloadWith(n)` simulates a reload that keeps the last n comments.

- [ ] Steps: write the harness and fixtures; add a trivial browser test that loads `producer_start.html` over CDP and finds the Go live button; run it in-container per Global Constraints (PASS); commit `test(fb): headless chromium harness and fixtures`.

---

### Task 3: Producer flows

**Files:** Create `fb/producer.py`, `tests/test_producer.py`

**Interfaces:**

```python
class Producer:
    def __init__(self, page: "Page", *, group_name: str, step_timeout_s: float, record_dir: Path | None = None): ...
    async def open(self, url: str) -> None
    async def is_login_page(self) -> bool                 # LOGIN_FORM_SELECTOR visible
    async def setup(self) -> StreamTarget                  # steps 1-4 + read key/server; raises StepFailed(step)
    async def wait_preview_ready(self, timeout_s: float) -> None   # Go live enabled (not disabled/aria-disabled)
    async def preview_lost(self) -> bool                   # PREVIEW_LOST_TEXT visible
    async def go_live(self, timeout_s: float) -> str       # click Go live, confirm interstitial if shown, wait END_LIVE_VIDEO; return live id from URL
    async def save_failure(self, step: str, directory: Path) -> None  # screenshot.png, page.html, aria.yml (locator("body").aria_snapshot())
```

The `setup()` steps, each a named step (`StepFailed(step, detail)` on timeout):
1. `group`: text matching `group_name` is visible.
2. `go_live_start`: click the role=button named by `GO_LIVE`.
3. `streaming_software`: click the first element matching `STREAMING_SOFTWARE` (role radio, button, or text).
4. `advanced`: click `ADVANCED_SETTINGS` if the persistent-key switch isn't yet visible.
5. `persistent_key`: locate the role=switch or checkbox named by `PERSISTENT_KEY`. If it's not checked (`aria-checked`/`is_checked`), click it and verify it becomes checked.
6. `end_on_stop` (soft): if a switch matching `END_LIVE_WHEN_STREAM_STOPS` exists and is unchecked, click it. Absence is not an error; log it at INFO.
7. `read_key`: collect the values of all `input` and `textarea` elements plus the text of `[data-testid], code, span` (bounded to 2000 nodes) via one `page.evaluate`, then `pick_stream_key`/`pick_server_url`. Either missing → `StepFailed("read_key")`.

When `record_dir` is set, save a screenshot and the aria snapshot after each step as `NN-step.png`/`.yml`. This is the recorder for the live runbook.

- [ ] Steps:
  1. Failing browser tests against the fixtures:
     - setup returns the fixture target and flips the persistent switch;
     - a missing key → `StepFailed("read_key")`;
     - `wait_preview_ready` times out without `__previewReady`, and passes after calling it;
     - `go_live` returns `1122334455667788` through the interstitial;
     - `is_login_page` is true on `login.html` and false elsewhere;
     - `save_failure` writes three files.
  2. FAIL.
  3. Implement.
  4. PASS (in-container).
  5. Commit `feat(fb): live producer flows`.

---

### Task 4: DOM comment capture

**Files:** Create `fb/watcher.js`, `fb/comments.py`, `tests/test_comments_browser.py`

**`watcher.js` contract:**
- It runs at document start via `add_init_script`, and only acts when `location.pathname` contains `/COMMENTS` or `window.__spForceWatch === true` (used by fixtures).
- On `DOMContentLoaded`, it collects every `[role=article]` in document order into a `rescan` batch.
- A `MutationObserver` on `document.body` (subtree, childList) collects newly added article nodes. It reports them as a `live` batch, debounced 250 ms, keeping DOM order and skipping nodes already reported (a `WeakSet`).
- Per article it extracts:
  - `author`: from `aria-label` via a JS copy of `COMMENT_LABEL_RE`, else the first `a[role=link]` text;
  - `text`: the article's text minus the author, trimmed, with whitespace collapsed;
  - `fb_comment_id`: from any `a[href*="comment_id="]`;
  - `created_at`: from a `<abbr data-utime>` or `time[datetime]` if present, else null.
- It calls `window.__spComment(JSON.stringify({kind, items}))`.
- The JS regexes and attribute names are **generated from `fb/selectors.py`**: `comments.py` renders `watcher.js` from a template with `str.replace` placeholders (`__ARTICLE_SELECTOR__`, `__LABEL_RE__`, `__COMMENT_ID_RE__`), so selectors stay in one file.

**`comments.py`:**

```python
class FbDomCapture:
    def __init__(self, context: "BrowserContext"): ...
    async def attach(self, page: "Page", log: CommentLog) -> None   # expose_binding("__spComment") once per context, add_init_script(rendered watcher), then page.reload() if already loaded
    async def detach(self) -> None
```

The binding callback parses the JSON, builds `Comment` objects, and calls `log.ingest_dom(items, rescan=(kind == "rescan"))`. Any exception is logged, never raised into the page.

- [ ] Steps:
  1. Failing browser tests on `comments_popout.html` (with `__spForceWatch`):
     - the initial rescan logs all fixture comments;
     - `__addComment` twice with the same author and text (no ID) logs 2 more;
     - `page.reload()` plus `__reloadWith(3)` logs nothing duplicated (rescan alignment);
     - an ID'd comment re-rendered after a reload is not duplicated;
     - the binding survives a reload.
  2. FAIL.
  3. Implement.
  4. PASS (in-container).
  5. Commit `feat(fb): DOM comment watcher and capture`.

---

### Task 5: SSN chat listener

**Files:** Create `ssn_listener.py`, `tests/test_ssn_listener.py`

**Interfaces:**

```python
def parse_ssn_message(raw: str) -> Comment | None   # JSON; accept a top-level message or {"msg"/"overlayNinja"/"data": {...}} wrapper;
                                                     # author = chatname, text = chatmessage with HTML tags stripped and entities unescaped;
                                                     # fb_comment_id = str(id) if present; created_at = str(timestamp) if present;
                                                     # return None unless chatname and chatmessage are both non-empty
class SsnListener:
    def __init__(self, session_id: str, log_factory: Callable[[], CommentLog | None],
                 url_template: str = "wss://io.socialstream.ninja/join/{session}/4/1", connect=websockets.connect): ...
    async def run(self) -> None   # connect, for each message -> parse -> log.append_ssn; reconnect with backoff 1,2,4..30 s forever
```

`log_factory` returns the current stream's log, or None when not live (messages are dropped then).

- [ ] Steps:
  1. Failing tests:
     - `parse_ssn_message` on plain, wrapped, HTML-entity, and empty payloads;
     - `SsnListener` against a local `websockets.serve` server on 127.0.0.1 (pass `url_template="ws://127.0.0.1:{port}/{session}"`, a test-only format) delivers two messages into a `CommentLog` and reconnects after the server closes once.
  2. FAIL.
  3. Implement.
  4. PASS.
  5. Commit `feat(ssn): chat listener`.

---

### Task 6: CdpBrowser and adapters

**Files:** Create `browser.py`, `fb/adapter.py`, `tests/test_adapter.py`; modify `app.py` (wire the real adapters)

**Interfaces:**

```python
class CdpBrowser:
    def __init__(self, cfg: CdpCfg): ...
    async def connect(self) -> None                      # async_playwright().start(); chromium.connect_over_cdp(cfg.url); context = browser.contexts[0]
    @property
    def context(self) -> "BrowserContext"
    async def dashboard_page(self) -> "Page"             # first non-extension, non-app page in context; create if none
    async def open_app_window(self, url: str, timeout_s: float = 20) -> "Page"
        # spawn [cfg.chromium_bin, f"--user-data-dir={cfg.user_data_dir}", f"--app={url}"] (hands off to running instance; process exits);
        # wait for context "page" event whose url startswith url's scheme+host+path prefix; return it
    async def find_page(self, url_prefix: str) -> "Page | None"
    async def close_browser(self) -> None                # CDP session Browser.close; swallow errors (already closed)
    async def http_reachable(self, url: str = "https://www.facebook.com/", timeout_s: float = 5) -> bool  # stdlib urllib in a thread; any HTTP status < 500 is reachable

class FacebookAdapter(FacebookPort):
    def __init__(self, cfg: Config, browser: CdpBrowser, *, record_dir: Path | None = None): ...
    # connect -> browser.connect; reachable -> http_reachable; logged_in -> open producer_url on dashboard page, not is_login_page
    # setup_live/wait_preview_ready/preview_lost/go_live -> Producer on dashboard page
    # open_comments(live_id) -> browser.open_app_window(cfg.facebook.comments_url_template.format(live_id=live_id)); store page
    # park_dashboard -> dashboard.goto("about:blank")
    # save_failure -> Producer.save_failure; close_browser -> browser.close_browser

class CaptureAdapter(CommentCapturePort):
    def __init__(self, cfg: Config, browser: CdpBrowser): ...
    # start(live_id, stream_dir): log = CommentLog(stream_dir/"comments.jsonl", live_id); find popout page (url contains f"/{live_id}/COMMENTS"),
    #   FbDomCapture.attach(page, log); if cfg.ssn.enabled and session_id: start SsnListener task (log_factory returns current log)
    # stop(): detach DOM capture, cancel the SSN task, keep the file
```

Also, after `connect()`, `CdpBrowser` keeps the SSN helper tab open (Global Constraints). The memory guard's `auto_reload_popout` stays unimplemented; it is off by spec.

`app.build_app`: when `facebook`/`capture` is None, construct `CdpBrowser(cfg.cdp)`, `FacebookAdapter(cfg, browser, record_dir=...)`, and `CaptureAdapter(cfg, browser)`. Add a `run --record DIR` CLI flag that sets `record_dir`. If a popout page closes while live (Chromium crash/restart), the controller's `Tick` handling in a live state calls `capture.start(live_id, stream_dir)` again when `CaptureAdapter.healthy()` is False. Add `healthy() -> bool` to `CaptureAdapter` and a guarded check in the controller (`hasattr`), and cover it with a fake in `test_controller.py`.

- [ ] Steps:
  1. Failing tests:
     - `test_adapter.py` (browser-marked) runs the full flow against fixtures with a fake config pointing `producer_url_template` and `comments_url_template` at the fixture server: `logged_in`, `setup_live`, `wait_preview_ready` (call `__previewReady` via the page), `go_live`, `open_comments` (the app window opens the popout fixture), `capture.start` logs comments, `park_dashboard`, `close_browser`.
     - Plus the controller test for capture re-attach.
  2. FAIL.
  3. Implement.
  4. PASS: in-container browser tests, plus the host unit suite (`cd stream-pilot && .venv/bin/pytest -q`).
  5. Commit `feat(fb): CDP browser, Facebook and capture adapters wired into run`.

---

### Task 7: Final integration build and live-validation runbook

**Files:** Create `docs/superpowers/runbooks/facebook-live-validation.md`; modify `README.md` (link the runbook)

- [ ] **Step 1: Rebuild and verify the image:**

```bash
bluebuild generate -o /tmp/obs-blue.Containerfile recipes/recipe.yml
podman build -f /tmp/obs-blue.Containerfile -t localhost/obs-blue:dev .
bash tests/image/smoke.sh localhost/obs-blue:dev
# then run the in-container browser tests (Global Constraints command); then: cd stream-pilot && .venv/bin/pytest -q
podman image prune -f
```

  All of it must pass.
- [ ] **Step 2: Write the runbook** for the user. It covers:
  1. Create a private test Group (only you as member).
  2. Put its IDs in `site.toml` as `[facebook] group_id`/`group_name`.
  3. `stream-pilot run --dry-run --record ~/fb-record` on the stream PC, after stopping the service: `systemctl --user stop stream-pilot`.
  4. Inspect `~/fb-record/*.png|.yml`. If any step failed, the failure folder shows what Facebook renders, and the fix goes **only** in `fb/selectors.py`.
  5. A full test-Group go-live: check that the popout opens in the chat column, `comments.jsonl` fills, and the parked dashboard doesn't affect the stream.
  6. Long-press to end: check that Facebook auto-ends.
  7. Power-button end.
  8. Check `report.txt`.
  9. The phase 2 criterion (3 clean streams, then remove SSN).
  10. Revert `site.toml` to the real group.

  It also includes the hardware checks from the spec's "Verify on hardware" table, with the exact commands:
  - `obs` log lines for the VAAPI encoder,
  - `systemd-cgls --user` for memory settings,
  - `cat /proc/sys/vm/swappiness`,
  - `stream-pilot status`,
  - `journalctl --user -u stream-pilot -f`.
- [ ] **Step 3: Commit** `docs: facebook live validation runbook`.
