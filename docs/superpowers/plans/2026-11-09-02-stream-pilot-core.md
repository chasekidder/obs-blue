# stream-pilot Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the stream-pilot Python package (everything except the Facebook/browser adapters). It must be fully unit-tested with fakes and able to drive OBS, a Stream Deck, the power key, and cameras on real hardware.

**Architecture:** An asyncio `Controller` consumes events (deck presses, power key, OBS stream-state changes, retry signal, ticks) from one queue. It calls narrow adapter protocols (`ObsPort`, `FacebookPort`, `CommentCapturePort`, `DeckPort`, `SystemPort`, `CameraPort`). It persists its state to `state.json` and writes `status.json` for the CLI and the health check. The adapters are thin. All logic lives in the controller and pure modules.

**Tech Stack:** Python 3.12+ (host has 3.14; the image has Fedora 44's python3), pytest + pytest-asyncio, simpleobsws, streamdeck (python-elgato-streamdeck), Pillow, evdev (from the Fedora RPM inside the image only), tomllib (stdlib).

**Spec:** `docs/superpowers/specs/2026-11-09-stream-pilot-design.md`. Read it first.

## Global Constraints

- Package lives at `stream-pilot/` (repo root), src layout: `stream-pilot/src/stream_pilot/`, tests in `stream-pilot/tests/`.
- Host safety: never use `sudo`, never run `rpm-ostree`/`bootc`/`systemctl` (system or `--user`) against the host, never install anything outside `stream-pilot/.venv`, `/tmp`, or rootless podman storage. Never `git push`.
- Never read secrets from `orig_scripts/` into committed files. Never commit anything under `orig_scripts/` or `site/`.
- `evdev`, `streamdeck`, `simpleobsws`, and `playwright` are imported lazily inside adapter functions, so the core and the unit tests import cleanly without them.
- Unit tests must not touch the network, real devices, `/proc` of the host (use fixture text), or the user's home (use `tmp_path`).
- Stream Deck key indices (6-key Mini, row-major): `0` status/go-live, `1` autofocus, `2` main cam, `3` retry, `4` spare/MEM, `5` jewelry cam.
- The OBS scene names are exactly `Main Camera`, `Jewlery Camera` (sic, existing spelling), and `Black`.
- Facebook auto-ends the live when OBS sends Stop Streaming. stream-pilot must never stop the OBS stream while `LIVE`/`RECONNECTING`, except on power-button or long-press.
- Commit after each task with a conventional message. Run `pytest` before each commit.

---

## File Structure

```
stream-pilot/
  pyproject.toml
  src/stream_pilot/
    __init__.py
    __main__.py          # CLI (argparse): run, status, retry, health, seed-obs, chromium-prep
    config.py            # dataclasses + TOML merge
    states.py            # State enum, KeyFace, STATUS_FACES
    events.py            # event dataclasses
    ports.py             # Protocols + StreamTarget + StepFailed
    persist.py           # StateStore (state.json), StatusWriter (status.json)
    comment_log.py       # Comment, CommentLog (dedupe), build_report
    controller.py        # Controller state machine
    obs.py               # SimpleObsClient (ObsPort) + map_stream_state()
    deck.py              # render_face(), StreamDeckAdapter (DeckPort)
    power.py             # PowerKeyListener (evdev)
    system.py            # SystemCtl (SystemPort), V4l2Camera (CameraPort)
    memory.py            # parse_meminfo/parse_psi/sample_processes, evaluate()
    seed_obs.py          # seed OBS config + websocket password
    chromium_prep.py     # fix Preferences crash flags
    health.py            # status/health CLI helpers
    app.py               # wiring for `run`
  tests/
    conftest.py
    fakes.py
    test_config.py test_comment_log.py test_persist.py test_controller.py
    test_obs.py test_deck.py test_power.py test_memory.py
    test_seed_obs.py test_chromium_prep.py test_health.py test_cli.py
```

---

### Task 1: Project skeleton

**Files:**
- Create: `stream-pilot/pyproject.toml`, `stream-pilot/src/stream_pilot/__init__.py`, `stream-pilot/src/stream_pilot/__main__.py`, `stream-pilot/tests/conftest.py`, `stream-pilot/tests/test_cli.py`
- Modify: `.gitignore` (add `stream-pilot/.venv/`, `**/__pycache__/`, `.pytest_cache/`)

**Interfaces:**
- Produces: console script `stream-pilot` → `stream_pilot.__main__:main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "stream-pilot"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[project.optional-dependencies]
runtime = [
  "simpleobsws==1.4.2",
  "streamdeck==0.9.6",
  "playwright==1.55.0",
  "websockets>=12",
  "Pillow>=10",
]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "Pillow>=10", "websockets>=12"]

[project.scripts]
stream-pilot = "stream_pilot.__main__:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
stream_pilot = ["fb/*.js", "data/*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

Pin each runtime version to the newest release available at implementation time (check with `pip index versions <pkg>`), and record the chosen versions in the commit message. The versions above are placeholders for that check, and the exact values must be updated.

- [ ] **Step 2: Write a failing CLI test** (`tests/test_cli.py`)

```python
from stream_pilot.__main__ import main

def test_help_exits_zero(capsys):
    try:
        main(["--help"])
    except SystemExit as e:
        assert e.code == 0
    out = capsys.readouterr().out
    for cmd in ("run", "status", "retry", "health", "seed-obs", "chromium-prep"):
        assert cmd in out
```

- [ ] **Step 3: Create the venv and run the test (expect FAIL)**

```bash
cd stream-pilot && python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]' && .venv/bin/pytest -q
```

- [ ] **Step 4: Implement `__main__.py` with argparse subcommands.** Each subcommand dispatches to a function that later tasks fill in. Until then, each function prints `not implemented` and returns 2. `run` accepts `--dry-run`. `health` accepts `--timeout SECONDS` (default 300). `main` returns the int exit code, and `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 5: Run the tests (PASS), then commit** `feat(stream-pilot): project skeleton and CLI`.

---

### Task 2: Config

**Files:** Create `src/stream_pilot/config.py`, `src/stream_pilot/data/default-config.toml`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `load_config(paths: list[Path]) -> Config`. Merges the TOML files in order; later files win, and nested tables are merged key by key.
  - `DEFAULT_PATHS = [Path("/etc/stream-pilot/config.toml"), Path("~/.config/stream-pilot/site.toml").expanduser(), Path("~/.config/stream-pilot/secrets.toml").expanduser()]`. Missing files are skipped.
  - `Config` has these frozen dataclass fields:
    - `facebook: FacebookCfg(actor_id: str, group_id: str, group_name: str, producer_url_template: str, comments_url_template: str)` with property `producer_url`.
    - `obs: ObsCfg(host: str, port: int, password: str)`
    - `cdp: CdpCfg(url: str, user_data_dir: Path, chromium_bin: str)`
    - `ssn: SsnCfg(enabled: bool, session_id: str)`
    - `cameras: dict[str, CameraCfg(scene: str, device: str)]` with keys `main`, `jewelry`.
    - `memory: MemoryCfg(interval_s: int, floor_mb: int, popout_rss_warn_mb: int, auto_reload_popout: bool)`
    - `deck: DeckCfg(long_press_s: float, brightness: int)`
    - `timeouts: TimeoutsCfg(step_s: float, attempts: int, preview_s: float, live_confirm_s: float, stop_s: float, fb_end_wait_s: float, net_retry_s: float, reconnect_retry_s: float)`
    - `paths: PathsCfg(state_dir: Path, streams_dir: Path, failures_dir: Path)`
  - `Config.missing_site_values() -> list[str]`: the names of the required site values that are empty (`facebook.actor_id`, `facebook.group_id`, `facebook.group_name`).

- [ ] **Step 1: Write `data/default-config.toml`.** Every key has a default. Path values use `~` and are expanded at load time.

```toml
[facebook]
actor_id = ""
group_id = ""
group_name = ""
producer_url_template = "https://www.facebook.com/live/producer/?actor={actor_id}&entry_point=group&target_id={group_id}"
comments_url_template = "https://www.facebook.com/live/producer/dashboard/{live_id}/COMMENTS/"

[obs]
host = "127.0.0.1"
port = 4455
password = ""

[cdp]
url = "http://127.0.0.1:9222"
user_data_dir = "~/.local/share/stream-pilot/chromium"
chromium_bin = "/usr/bin/chromium-browser"

[ssn]
enabled = true
session_id = ""

[cameras.main]
scene = "Main Camera"
device = "/dev/video0"

[cameras.jewelry]
scene = "Jewlery Camera"
device = "/dev/video2"

[memory]
interval_s = 60
floor_mb = 600
popout_rss_warn_mb = 1500
auto_reload_popout = false

[deck]
long_press_s = 3.0
brightness = 70

[timeouts]
step_s = 30
attempts = 3
preview_s = 180
live_confirm_s = 60
stop_s = 10
fb_end_wait_s = 5
net_retry_s = 1
reconnect_retry_s = 5

[paths]
state_dir = "~/.local/state/stream-pilot"
streams_dir = "~/streams"
failures_dir = "~/.local/state/stream-pilot/failures"
```

`load_config` always loads this packaged file first (via `importlib.resources`), then `paths`.

- [ ] **Step 2: Write the failing tests:**
  - With defaults only, `cameras["jewelry"].scene == "Jewlery Camera"`.
  - A site file overriding `facebook.actor_id` and `cameras.main.device` merges and keeps the other keys.
  - `producer_url` formats both IDs into the URL.
  - `missing_site_values()` lists all three when empty and `[]` when filled.
  - `~` expands in the path values.
  - Missing files are ignored.
- [ ] **Step 3: Run (FAIL). Step 4: Implement with `tomllib` and a recursive dict merge, then build the dataclasses. Step 5: Run (PASS), then commit** `feat(stream-pilot): layered TOML config`.

---

### Task 3: States, events, ports, persistence

**Files:** Create `states.py`, `events.py`, `ports.py`, `persist.py`, `tests/test_persist.py`

**Interfaces (exact):**

```python
# states.py
from enum import StrEnum
from dataclasses import dataclass

class State(StrEnum):
    STARTING = "STARTING"; NO_NET = "NO_NET"; LOGIN = "LOGIN"; SETUP = "SETUP"
    READY = "READY"; GOING_LIVE = "GOING_LIVE"; LIVE = "LIVE"
    RECONNECTING = "RECONNECTING"; ENDING = "ENDING"; ERROR = "ERROR"

@dataclass(frozen=True)
class KeyFace:
    label: str
    bg: str            # "#rrggbb"
    fg: str = "#ffffff"
    blink: bool = False

STATUS_FACES: dict[State, KeyFace] = {
    State.STARTING: KeyFace("BOOT", "#555555"),
    State.NO_NET: KeyFace("NO NET", "#555555"),
    State.LOGIN: KeyFace("LOG IN", "#e67e00"),
    State.SETUP: KeyFace("SETUP", "#1f5fbf"),
    State.READY: KeyFace("GO LIVE", "#1e9e3e"),
    State.GOING_LIVE: KeyFace("GOING\nLIVE", "#1f5fbf", blink=True),
    State.LIVE: KeyFace("LIVE", "#c0141c"),
    State.RECONNECTING: KeyFace("RECON-\nNECT", "#c0141c", blink=True),
    State.ENDING: KeyFace("ENDING", "#c0141c"),
    State.ERROR: KeyFace("ERROR", "#e67e00"),
}
LIVE_STATES = frozenset({State.LIVE, State.RECONNECTING})
```

```python
# events.py
from dataclasses import dataclass
@dataclass(frozen=True) class DeckPress: key: int; long: bool = False
@dataclass(frozen=True) class PowerPress: pass
@dataclass(frozen=True) class ObsStreamState: active: bool; reconnecting: bool = False
@dataclass(frozen=True) class RetryRequested: pass
@dataclass(frozen=True) class Tick: pass
Event = DeckPress | PowerPress | ObsStreamState | RetryRequested | Tick
```

```python
# ports.py
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from .events import Event

EventSink = Callable[[Event], None]   # must be thread-safe (adapters use loop.call_soon_threadsafe)

@dataclass(frozen=True)
class StreamTarget: server: str; key: str

class StepFailed(Exception):
    def __init__(self, step: str, detail: str = ""):
        super().__init__(f"{step}: {detail}"); self.step = step; self.detail = detail

class ObsPort(Protocol):
    async def connect(self) -> None: ...
    def set_event_sink(self, sink: EventSink) -> None: ...
    async def stream_active(self) -> bool: ...
    async def set_stream_target(self, target: StreamTarget) -> None: ...
    async def start_stream(self) -> None: ...
    async def stop_stream(self) -> None: ...
    async def set_scene(self, name: str) -> None: ...
    async def current_scene(self) -> str: ...

class FacebookPort(Protocol):
    async def connect(self) -> None: ...                     # attach to Chromium over CDP
    async def reachable(self) -> bool: ...                   # https://www.facebook.com answers
    async def logged_in(self) -> bool: ...                   # opens producer URL, True if not a login page
    async def setup_live(self) -> StreamTarget: ...          # spec "Facebook setup" steps 1-5 (before OBS start)
    async def wait_preview_ready(self, timeout_s: float) -> None: ...  # raises StepFailed("preview")
    async def preview_lost(self) -> bool: ...
    async def go_live(self, timeout_s: float) -> str: ...    # returns live_id; raises StepFailed("go_live")
    async def open_comments(self, live_id: str) -> None: ...
    async def park_dashboard(self) -> None: ...
    async def save_failure(self, step: str, directory: Path) -> None: ...
    async def close_browser(self) -> None: ...

class CommentCapturePort(Protocol):
    async def start(self, live_id: str, stream_dir: Path) -> None: ...
    async def stop(self) -> None: ...

class DeckPort(Protocol):
    def set_event_sink(self, sink: EventSink) -> None: ...
    def show(self, key: int, face: "KeyFace") -> None: ...
    async def start(self) -> None: ...

class PowerPort(Protocol):
    def set_event_sink(self, sink: EventSink) -> None: ...
    async def start(self) -> None: ...

class SystemPort(Protocol):
    async def poweroff(self) -> None: ...
    async def stop_user_unit(self, unit: str) -> None: ...

class CameraPort(Protocol):
    async def set_autofocus(self, device: str, on: bool) -> None: ...
```

```python
# persist.py
@dataclass
class Saved: state: str; live_id: str | None = None; stream_dir: str | None = None
class StateStore:
    def __init__(self, path: Path): ...
    def load(self) -> Saved | None: ...   # None if missing or corrupt (log a warning, never raise)
    def save(self, saved: Saved) -> None: ...  # atomic: write tmp in same dir, os.replace
class StatusWriter:
    def __init__(self, path: Path): ...
    def write(self, *, state: str, since: float, last_error: str | None,
              live_id: str | None, memory: dict | None) -> None: ...  # atomic JSON, adds "updated_at": time.time()
```

- [ ] **Step 1: Write the failing tests** for `StateStore`:
  - a round trip through `save` and `load`,
  - corrupt JSON returns `None`,
  - a missing file returns `None`,
  - `save` never leaves a `.tmp` file behind.

  And for `StatusWriter`: the written JSON has every key plus `updated_at`.
- [ ] **Step 2: Run (FAIL). Step 3: Implement all four modules exactly as above. Step 4: PASS, then commit** `feat(stream-pilot): states, events, ports, persistence`.

---

### Task 4: Comment log with repeat-safe dedupe

**Files:** Create `comment_log.py`, `tests/test_comment_log.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class Comment:
    author: str
    text: str
    fb_comment_id: str | None = None
    created_at: str | None = None
    def key(self) -> tuple[str, str]: return (self.author.strip().casefold(), " ".join(self.text.split()))

class CommentLog:
    def __init__(self, path: Path, live_id: str, clock: Callable[[], float] = time.time): ...
    def append_ssn(self, c: Comment) -> None: ...
    def ingest_dom(self, batch: list[Comment], *, rescan: bool) -> int: ...  # returns lines appended
    gaps: list[dict]   # {"at": float, "batch_size": int}
def build_report(path: Path, window_s: float = 120.0) -> str: ...
```

Each JSONL line is `{"captured_at": float, "created_at": str|None, "source": "fb_dom"|"ssn", "fb_comment_id": str|None, "author": str, "text": str, "live_id": str}`. A gap is also written as a line, `{"source": "gap", "captured_at": ..., "batch_size": n, "live_id": ...}`. Each line is flushed with `os.fsync` after writing.

Dedupe rules (from the spec):
1. A `fb_dom` comment with `fb_comment_id`: append iff the ID hasn't been seen.
2. No ID and `rescan=False` (a live mutation): always append.
3. No ID and `rescan=True`:
   - Compare by `key()`. Let `tail` be the last N `fb_dom` keys, where `N = len(batch)`.
   - Find the largest `k ≥ 1` such that some `i` has `tail[-k:] == batch_keys[i-k:i]`. Among equal `k`, pick the largest `i`.
   - Append `batch[i:]`.
   - If there is no such `k` (and the tail isn't empty), append the whole batch and record a gap.
   - If the tail is empty, append the whole batch with no gap.
   - ID'd comments in a rescan batch still follow rule 1 for whether they are appended, but they still take part in the alignment.

Reference implementation of the alignment (use it as written):

```python
def _new_suffix_start(tail: list[tuple], batch: list[tuple]) -> int | None:
    best_k, best_i = 0, None
    for i in range(len(batch), 0, -1):
        k = 0
        while k < i and k < len(tail) and tail[-1 - k] == batch[i - 1 - k]:
            k += 1
        if k > best_k:
            best_k, best_i = k, i
    return best_i  # None => no overlap
```

- [ ] **Step 1: Write the failing tests** (all with `tmp_path` and a fake clock):
  - `test_live_repeats_kept`: two live mutations of `("ann","sold")` append 2 lines.
  - `test_rescan_skips_overlap`: the log has a, b, c. A rescan of `[b, c, d]` appends only d.
  - `test_rescan_keeps_true_repeat`: the log has a, sold(ann). A rescan of `[a, sold(ann), sold(ann)]` appends exactly one more `sold(ann)`.
  - `test_rescan_no_new`: a rescan of `[b, c]` when the log ends with b, c appends 0.
  - `test_rescan_gap`: the log has x, y. A rescan of `[p, q]` appends 2 and records one gap line.
  - `test_ids_dedupe`: the same `fb_comment_id` twice appends once, and a different ID with the same text appends.
  - `test_ssn_never_deduped`: `append_ssn` twice with the same comment appends 2.
  - `test_report`: SSN and DOM entries with the same key within 120 s match. The report lists each "only in ssn" and "only in fb_dom" entry, and counts the gaps.
- [ ] **Step 2: Run (FAIL). Step 3: Implement. Step 4: PASS, then commit** `feat(stream-pilot): comment log with repeat-safe dedupe and report`.

---

### Task 5: Controller state machine

**Files:** Create `controller.py`, `tests/fakes.py`, `tests/test_controller.py`

**Interfaces:**

```python
class Controller:
    def __init__(self, cfg: Config, *, obs: ObsPort, fb: FacebookPort, capture: CommentCapturePort,
                 deck: DeckPort, system: SystemPort, camera: CameraPort,
                 store: StateStore, status: StatusWriter, dry_run: bool = False,
                 clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep): ...
    state: State
    live_id: str | None
    last_error: str | None
    def sink(self, event: Event) -> None        # thread-safe enqueue (uses loop.call_soon_threadsafe if off-loop)
    async def boot(self) -> None                 # resume-or-start; spawns the setup task when appropriate
    async def handle(self, event: Event) -> None # one event (tests call this directly)
    async def run(self) -> None                  # boot() then loop over queue forever
    async def wait_idle(self) -> None            # test helper: await the current background flow task, if any
```

Behavior (implement exactly, and cover each bullet with a test using the fakes):

- **Transitions:** every transition saves `Saved(state, live_id, stream_dir)` and writes the status, then updates deck key 0 with `STATUS_FACES[state]`. In `ERROR` the label is `"ERR\n" + step[:6].upper()`.
- **`boot()` resume:**
  - If `store.load()` says `LIVE`/`RECONNECTING` with a `live_id`, and `obs.stream_active()` is True, go to `LIVE` and call `capture.start(live_id, stream_dir)`.
  - Never call `fb.setup_live`, `obs.start_stream`, or `obs.stop_stream` in this path.
  - Otherwise start the setup flow.
- **Setup flow (a background task; only one at a time):**
  1. `STARTING`: `obs.connect()` and `fb.connect()`, retried every 2 s until both succeed.
  2. If `cfg.missing_site_values()` is non-empty → `ERROR` with step `config`.
  3. `NO_NET`: loop on `fb.reachable()` every `net_retry_s`.
  4. `LOGIN`: if not `fb.logged_in()`, stay in `LOGIN` and re-check every 5 s.
  5. `SETUP`:
     - `target = fb.setup_live()`, then `obs.set_stream_target(target)`.
     - If `obs.stream_active()` already, `obs.stop_stream()` first. This is safe, since no live exists in `SETUP`.
     - `obs.start_stream()`, then `fb.wait_preview_ready(preview_s)`.
  6. `READY`.
- **Step retries:** each FB step is wrapped as up to `attempts` tries. On the final `StepFailed`, call `fb.save_failure(step, failures_dir/<timestamp>-<step>)`, set `last_error`, and enter `ERROR`.
- **While `READY`:** on each `Tick`, if `fb.preview_lost()`, re-run the setup flow from `SETUP`.
- **`DeckPress(0)` short press:**
  - In `READY`: if `dry_run`, log `dry-run: would go live` and stay `READY`. Otherwise:
    1. Enter `GOING_LIVE` and call `live_id = fb.go_live(live_confirm_s)`.
    2. Create `streams_dir/<YYYY-mm-dd_HHMM>-<live_id>/`.
    3. `fb.open_comments(live_id)`, then `capture.start(live_id, dir)`, then `fb.park_dashboard()`.
    4. Enter `LIVE`.

    On `StepFailed` in `go_live`, enter `ERROR`. The OBS stream keeps running, so a retry can return to `READY` via setup.
  - In `ERROR`: re-run the setup flow from `LOGIN`.
  - Ignored in any other state.
- **`DeckPress(0, long=True)` in a live state:** run the end flow with `power_off=False`, then the setup flow.
- **`DeckPress(3)` (retry) / `RetryRequested`:** in any state except `GOING_LIVE`/`LIVE`/`RECONNECTING`/`ENDING`, cancel the running flow and restart setup from `LOGIN`.
- **`DeckPress(2)` / `DeckPress(5)`:**
  - `obs.set_scene(cameras[main|jewelry].scene)`, then `camera.set_autofocus(device, True)`.
  - Remember the active camera, and show autofocus ON on key 1 (`KeyFace("AF\nON", "#1e9e3e")`, or OFF `KeyFace("AF\nOFF", "#555555")`).
- **`DeckPress(1)`:** toggle autofocus on the active camera's device and update key 1.
- **`ObsStreamState`:**
  - In `LIVE` with `reconnecting=True` → `RECONNECTING`.
  - In `RECONNECTING` with `active=True, reconnecting=False` → `LIVE`.
  - In `LIVE`/`RECONNECTING` with `active=False` (OBS gave up) → `RECONNECTING`, and start a task that calls `obs.start_stream()` every `reconnect_retry_s` until `obs.stream_active()` is True, then → `LIVE`.
  - Never call `stop_stream` here.
  - In other states, ignore.
- **`PowerPress`:**
  - In live states: end flow with `power_off=True`:
    1. `ENDING`, `obs.stop_stream()`, and wait up to `stop_s` for `stream_active()` to be False.
    2. Sleep `fb_end_wait_s`, `capture.stop()`, and write `report.txt` via `build_report`.
    3. `fb.close_browser()`, `system.stop_user_unit("obs.service")`, `system.poweroff()`.

    Errors in any step are logged and the remaining steps still run.
  - In any other state: `fb.close_browser()`, `stop_user_unit("obs.service")`, `poweroff()`. Ignore a second `PowerPress` while `ENDING`.
- **Key 3 face:** always `KeyFace("RETRY", "#333333")`. Key 4 is managed by the memory guard (Task 9), blank `KeyFace("", "#000000")` by default.

`tests/fakes.py` provides `FakeObs`, `FakeFacebook`, `FakeCapture`, `FakeDeck`, `FakeSystem`, and `FakeCamera`. Each records calls in `self.calls: list[tuple]` and has configurable return values and exceptions (e.g. `FakeFacebook(logged_in=[False, True], setup_raises=StepFailed("setup"))`). `FakeObs.active` is a settable bool. Fakes use an instant `sleep` (tests pass `sleep=fake_sleep` that just yields).

- [ ] **Step 1: Write `tests/fakes.py` and the failing tests** in `test_controller.py`, one per behavior bullet above. At minimum:
  - happy path to `READY`; go live; power-off while live (with the exact call order asserted);
  - resume while live (asserting that no setup/start/stop calls happen);
  - login wait; config missing → `ERROR`;
  - step failure → `ERROR` with `save_failure` called; retry from `ERROR`;
  - reconnecting via OBS events; OBS gave up → restart loop with no `stop_stream`;
  - the dry run doesn't call `go_live`; a long-press ends without power-off;
  - camera keys and autofocus toggle; retry is ignored while `LIVE`.
- [ ] **Step 2: Run (FAIL). Step 3: Implement `controller.py`. Step 4: PASS. Run all tests. Commit** `feat(stream-pilot): controller state machine`.

---

### Task 6: OBS adapter

**Files:** Create `obs.py`, `tests/test_obs.py`

**Interfaces:**
- `map_stream_state(event_data: dict) -> ObsStreamState | None` maps OBS websocket v5 `StreamStateChanged` `outputState`:
  - `OBS_WEBSOCKET_OUTPUT_STARTED` → `(True, False)`
  - `OBS_WEBSOCKET_OUTPUT_RECONNECTING` → `(True, True)`
  - `OBS_WEBSOCKET_OUTPUT_RECONNECTED` → `(True, False)`
  - `OBS_WEBSOCKET_OUTPUT_STOPPED` → `(False, False)`
  - anything else → `None`
- `class SimpleObsClient(ObsPort)`: `__init__(self, cfg: ObsCfg)`. Uses `simpleobsws.WebSocketClient(url=f"ws://{host}:{port}", password=...)`, with these requests:
  - `GetStreamStatus` → `outputActive`
  - `SetStreamServiceSettings` with `{"streamServiceType": "rtmp_custom", "streamServiceSettings": {"server": t.server, "key": t.key}}`
  - `StartStream`, `StopStream`
  - `SetCurrentProgramScene {"sceneName": name}`, `GetCurrentProgramScene` → `currentProgramSceneName` (fallback `sceneName`)
  - Registers an event callback for `StreamStateChanged` that calls the sink with the mapped event.
  - Reconnects automatically: on a request failure due to disconnect, retry connect once, then raise `StepFailed("obs")`.
  - Simpleobsws `connect()` then `wait_until_identified()`.

- [ ] Steps:
  1. Failing tests for `map_stream_state`, plus a test that `SimpleObsClient.set_stream_target` builds the exact request payload (inject a fake `ws` object via a constructor kwarg `client=`).
  2. Run (FAIL).
  3. Implement.
  4. PASS.
  5. Commit `feat(stream-pilot): obs-websocket adapter`.

---

### Task 7: Stream Deck adapter

**Files:** Create `deck.py`, `tests/test_deck.py`

**Interfaces:**
- `render_face(face: KeyFace, size: tuple[int, int], blink_phase: bool = False) -> PIL.Image.Image`:
  - Solid `bg`, or black when `face.blink and blink_phase`.
  - Multiline `label`, centered in `fg`.
  - Font: the first existing of `/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf` and `/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf`, else `ImageFont.load_default(size=...)`. The size shrinks until the text fits within 90% of the width.
- `class StreamDeckAdapter(DeckPort)`: `__init__(self, cfg: DeckCfg)`.
  - `start()` runs a background task that enumerates via `StreamDeck.DeviceManager.DeviceManager().enumerate()` every 5 s until a deck is found.
  - It opens the deck, resets it, sets the brightness, and re-renders all remembered faces.
  - It registers a key callback that records the press time on down. On up it emits `DeckPress(key, long=(held >= long_press_s))` via the sink. Callbacks run on the library's thread, so it uses `loop.call_soon_threadsafe`.
  - A 0.5 s blink ticker re-renders blinking keys.
  - `show()` stores the face and renders it if a deck is open.
  - On a transport error it closes the deck and goes back to enumerating (hotplug).
- [ ] Steps:
  1. Failing tests: `render_face` returns the right size; blink phase is black; a long label fits. The press-duration logic is factored into a pure helper `press_event(key, down_at, up_at, long_press_s) -> DeckPress`, which is also tested.
  2. FAIL.
  3. Implement.
  4. PASS.
  5. Commit `feat(stream-pilot): stream deck adapter`.

---

### Task 8: Power key, system, and camera adapters

**Files:** Create `power.py`, `system.py`, `tests/test_power.py`

**Interfaces:**
- `power.find_power_devices(list_devices=evdev.list_devices, open_device=evdev.InputDevice) -> list`: devices whose `name == "Power Button"` and whose capabilities include `KEY_POWER`. The parameters are injectable for tests.
- `power.PowerKeyListener(PowerPort)`:
  - `start()` reads all of those devices with `async_read_loop()`, and emits `PowerPress()` on `EV_KEY`/`KEY_POWER` with value 1.
  - If none are found, it logs a warning and retries every 10 s.
  - `evdev` is imported inside the functions.
- `system.SystemCtl(SystemPort)`: `poweroff()` runs `systemctl poweroff`, and `stop_user_unit(u)` runs `systemctl --user stop u`, both via `asyncio.create_subprocess_exec`. Log non-zero exits.
- `system.V4l2Camera(CameraPort)`: `set_autofocus(dev, on)` runs `v4l2-ctl -d dev --set-ctrl=focus_automatic_continuous=1|0`.
- [ ] Steps:
  1. Failing tests:
     - `find_power_devices` with fake device objects (name and capabilities);
     - a fake async event stream produces exactly one `PowerPress` for a value-1 event and none for values 0 and 2;
     - `SystemCtl`/`V4l2Camera` build the exact argv (inject `exec_fn`).
  2. FAIL.
  3. Implement.
  4. PASS.
  5. Commit `feat(stream-pilot): power key, system and camera adapters`.

---

### Task 9: Memory guard

**Files:** Create `memory.py`, `tests/test_memory.py`, `tests/data/meminfo.txt`, `tests/data/pressure_memory.txt`

**Interfaces:**
- `parse_meminfo(text) -> dict[str, int]` (kB values)
- `parse_psi(text) -> dict` (`{"some_avg60": float, "full_avg60": float}`)
- `sample_processes(proc_root: Path = Path("/proc")) -> list[ProcSample(pid, comm, rss_kb, label)]`:
  - The label is `obs` for comm `obs`, `kwin_wayland`, `plasmashell`.
  - For Chromium (`comm` startswith `chromium` or `chrome`), the label is `chromium-renderer` when the cmdline has `--type=renderer`, else `chromium-<type or browser>`.
  - Others are skipped.
- `take_sample(proc_root=Path("/proc")) -> dict` (`{"mem_available_mb", "psi", "procs": [...]}`)
- `evaluate(sample: dict, cfg: MemoryCfg) -> str | None`: returns `"MEM"` if `mem_available_mb < floor_mb` or any `chromium-renderer` RSS exceeds `popout_rss_warn_mb`, else `None`.
- `class MemoryGuard`: `__init__(cfg, deck: DeckPort, on_sample: Callable[[dict], None])`. Its `run()` loop samples every `interval_s`, logs the sample as one JSON log line (`logging.getLogger("stream_pilot.memory")`), calls `on_sample` (the controller stores it for `status.json`), and sets key 4 to `KeyFace("MEM", "#e67e00", blink=True)` or blank.
  - `auto_reload_popout` is read but **not acted on** in this plan. Plan 3 wires it, and it defaults to false.
- [ ] Steps:
  1. Failing tests using fixture files and a fake `/proc` tree built in `tmp_path` (`<pid>/comm`, `<pid>/cmdline` NUL-separated, `<pid>/status` with a `VmRSS:` line).
  2. FAIL.
  3. Implement.
  4. PASS.
  5. Commit `feat(stream-pilot): memory sampling and guard`.

---

### Task 10: OBS seeding and Chromium prep

**Files:** Create `seed_obs.py`, `chromium_prep.py`, `tests/test_seed_obs.py`, `tests/test_chromium_prep.py`

**Interfaces:**
- `seed_obs(defaults_dir: Path, config_dir: Path, secrets_path: Path, cameras: dict[str, CameraCfg]) -> bool` (True if it copied the defaults):
  1. If `config_dir` doesn't exist, `copytree(defaults_dir, config_dir)`. Then, in `config_dir/basic/scenes/*.json`, replace the literal strings `@MAIN_CAMERA_DEVICE@` and `@JEWELRY_CAMERA_DEVICE@` with `cameras["main"].device` and `cameras["jewelry"].device`.
  2. Ensure `secrets_path` (TOML) has `[obs] password`. Generate it with `secrets.token_urlsafe(24)` if missing, and set file mode 0600. Preserve other keys when rewriting.
  3. Always write `config_dir/plugin_config/obs-websocket/config.json` as `{"alerts_enabled": false, "auth_required": true, "first_load": false, "server_enabled": true, "server_password": <pw>, "server_port": 4455}`. This is idempotent.
- `prep_chromium_profile(user_data_dir: Path) -> bool`: if `user_data_dir/Default/Preferences` exists, load the JSON, set `profile.exit_type = "Normal"` and `profile.exited_cleanly = True`, and write it back atomically. Return whether anything changed. Corrupt JSON: log and return False, never raise.
- CLI:
  - `stream-pilot seed-obs` calls `seed_obs(Path("/usr/share/stream-pilot/obs"), ~/.config/obs-studio, ~/.config/stream-pilot/secrets.toml, cfg.cameras)`.
  - `stream-pilot chromium-prep` calls `prep_chromium_profile(cfg.cdp.user_data_dir)` and also `mkdir -p` the dir.
  - Both always exit 0 unless the defaults dir is missing (exit 1). They must never block a unit start.
- [ ] Steps:
  1. Failing tests: first seed copies and substitutes; a second seed doesn't overwrite the user's edits but refreshes the websocket json; password persists across runs; 0600 mode; Preferences fix; corrupt Preferences is tolerated.
  2. FAIL.
  3. Implement.
  4. PASS.
  5. Commit `feat(stream-pilot): obs seeding and chromium crash-flag prep`.

---

### Task 11: Health, status, and retry CLI

**Files:** Create `health.py`, `tests/test_health.py`; modify `__main__.py`

**Interfaces:**
- `format_status(status: dict) -> str`: human-readable state, time since the last update, last error, live ID, and `mem_available_mb`.
- `check_health(status_path: Path, timeout_s: float, now=time.time, sleep=time.sleep) -> int`: polls every 5 s until the timeout. Returns 0 once the status file exists, has `state != "STARTING"`, and has `updated_at` within the last 180 s. Otherwise returns 1.
- CLI:
  - `status` prints `format_status` (exit 1 if there's no file).
  - `health --timeout N` returns `check_health`.
  - `retry` runs `systemctl --user kill -s SIGUSR1 stream-pilot.service`.
- [ ] Steps: failing tests for `format_status` and `check_health` (fresh/stale/STARTING/missing, with fake time); FAIL; implement; PASS; commit `feat(stream-pilot): status, health and retry commands`.

---

### Task 12: App wiring for `run`

**Files:** Create `app.py`; modify `__main__.py`; create `tests/test_app.py`

**Interfaces:**
- `build_app(cfg: Config, *, dry_run: bool, facebook: FacebookPort | None = None, capture: CommentCapturePort | None = None) -> tuple[Controller, list[Awaitable]]`:
  - Builds the real adapters (SimpleObsClient, StreamDeckAdapter, PowerKeyListener, SystemCtl, V4l2Camera, StateStore(state_dir/"state.json"), StatusWriter(state_dir/"status.json"), MemoryGuard).
  - Wires every adapter's `set_event_sink(controller.sink)`.
  - Returns the controller plus the background coroutines (deck.start, power.start, memory guard run, a 15 s `Tick` ticker).
  - If `facebook`/`capture` is None, it uses `NotWiredFacebook`/`NotWiredCapture`, whose methods raise `StepFailed("facebook", "not wired")`. Plan 3 replaces these.
- `async def run_app(cfg, dry_run)`: configures logging to stderr (journald captures it) in the format `%(levelname)s %(name)s: %(message)s`. Installs a SIGUSR1 handler that calls `controller.sink(RetryRequested())`, plus SIGTERM/SIGINT handlers that cancel the tasks gracefully. Gathers everything.
- `stream-pilot run [--dry-run]` calls `asyncio.run(run_app(load_config(DEFAULT_PATHS), dry_run))`.
- [ ] Steps:
  1. Failing test: `build_app` with fakes injected for `facebook`/`capture` and monkeypatched adapter classes returns a Controller whose ports are wired, and the SIGUSR1 handler enqueues `RetryRequested`. Use `asyncio` with the fakes; never open devices.
  2. FAIL.
  3. Implement.
  4. PASS. Run the full suite: `cd stream-pilot && .venv/bin/pytest -q`. Every test must pass.
  5. Commit `feat(stream-pilot): app wiring and run command`.
