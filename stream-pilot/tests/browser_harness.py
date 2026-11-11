"""Local fixture server and headless Chromium for browser tests.

Nothing here touches the network beyond 127.0.0.1: the fixture pages are
hand-written HTML under ``tests/fixtures/fb`` and Chromium is a throwaway
headless instance with its own profile, HOME and XDG dirs.

Fixture URL mapping (``fixture_server``)
----------------------------------------
Plain files are served by name (``/producer_setup.html``). In addition, the
Facebook-shaped URLs below are mapped onto fixture files, so templates such as
``producer_url_template`` / ``comments_url_template`` can point at the fixture
server with realistic URL shapes (query strings are accepted everywhere)::

    /live/producer/                                -> producer_start.html
    /live/producer/dashboard/<id>/                 -> producer_live.html
    /live/producer/dashboard/<id>/COMMENTS/        -> comments_popout.html

``<id>`` is any run of digits (the fixtures use ``1122334455667788``).
Trailing slashes are optional. Fixture pages link to each other with absolute
paths so they work from any of these URLs.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import AsyncIterator

STARTUP_TIMEOUT = 20.0

_ROUTES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^/live/producer/?$"), "producer_start.html"),
    (re.compile(r"^/live/producer/dashboard/\d+/?$"), "producer_live.html"),
    (re.compile(r"^/live/producer/dashboard/\d+/COMMENTS/?$", re.I), "comments_popout.html"),
)


def map_fixture_path(path: str) -> str | None:
    """Fixture file name for a Facebook-shaped URL path (query already stripped), else None."""
    for rx, name in _ROUTES:
        if rx.match(path):
            return name
    return None


class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._remap()
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        self._remap()
        super().do_HEAD()

    def _remap(self) -> None:
        path, sep, query = self.path.partition("?")
        name = map_fixture_path(path)
        if name:
            self.path = "/" + name + (sep + query if sep else "")

    def log_message(self, format: str, *args: object) -> None:  # quiet
        pass


@contextlib.asynccontextmanager
async def fixture_server(directory: str | Path) -> AsyncIterator[str]:
    """Serve ``directory`` on 127.0.0.1 (free port) in a daemon thread; yield the base URL."""
    handler = functools.partial(_Handler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="fixture-server", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def chromium_bin() -> str:
    """``STREAM_PILOT_CHROMIUM`` if set, else the configured ``cdp.chromium_bin``."""
    env = os.environ.get("STREAM_PILOT_CHROMIUM")
    if env:
        return env
    from stream_pilot.config import load_config

    return load_config([]).cdp.chromium_bin


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chromium_env(home: str) -> dict[str, str]:
    """Own HOME and XDG dirs (HOME may be dangling in containers; keeps the real profile out)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env["HOME"] = home
    for var, sub in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_CACHE_HOME", ".cache"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_RUNTIME_DIR", "run"),
    ):
        path = os.path.join(home, sub)
        os.makedirs(path, mode=0o700, exist_ok=True)
        env[var] = path
    return env


def _fetch_version(url: str) -> dict:
    with urllib.request.urlopen(f"{url}/json/version", timeout=2) as resp:
        return json.load(resp)


class ChromiumUrl(str):
    """The CDP http URL (a plain ``str``), plus what a second Chromium process needs to
    hand off to this instance: ``profile_dir`` (its ``--user-data-dir``) and ``env``
    (the HOME/XDG environment it was launched with)."""

    profile_dir: str
    env: dict[str, str]


@contextlib.asynccontextmanager
async def headless_chromium(binary: str) -> AsyncIterator[ChromiumUrl]:
    """Launch headless Chromium with a temp profile and free debug port; yield the CDP http URL."""
    import asyncio

    port = _free_port()
    url = ChromiumUrl(f"http://127.0.0.1:{port}")
    tmp = tempfile.mkdtemp(prefix="stream-pilot-chromium-")
    url.profile_dir = os.path.join(tmp, "profile")
    url.env = _chromium_env(os.path.join(tmp, "home"))
    args = [
        binary,
        "--headless=new",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={url.profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
    ]
    if os.geteuid() == 0:  # containers run as root; Chromium refuses to start without this
        args.append("--no-sandbox")
    args.append("about:blank")
    proc = subprocess.Popen(
        args,
        env=url.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"Chromium exited early (code {proc.returncode})")
            try:
                await asyncio.to_thread(_fetch_version, url)
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{url}/json/version not reachable in {STARTUP_TIMEOUT:.0f}s") from None
                await asyncio.sleep(0.2)
        yield url
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 5)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.to_thread(proc.wait)
        shutil.rmtree(tmp, ignore_errors=True)
