"""CDP compatibility self-test.

Proves that the pinned Playwright can still attach to the image's system
Chromium over CDP, so a Chromium update cannot silently break stream-pilot.
Run in CI against the built image: ``stream-pilot selftest-cdp``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

CDP_PORT = 9333
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
STARTUP_TIMEOUT = 20.0


def _fail(message: str) -> int:
    print(f"selftest-cdp: FAIL: {message}", file=sys.stderr)
    return 1


def _chromium_args(user_data_dir: str) -> list[str]:
    args = [
        "--headless=new",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--disable-dev-shm-usage",
    ]
    if os.geteuid() == 0:  # container CI runs as root; Chromium refuses without this
        args.append("--no-sandbox")
    args.append("about:blank")
    return args


def _chromium_env(home: str) -> dict[str, str]:
    """Environment for the throwaway Chromium: its own HOME and XDG dirs.

    Keeps the self-test away from the real profile and works where $HOME is
    missing or dangling (rootless ostree containers: /root -> var/roothome).
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env["HOME"] = home
    for var, sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_CACHE_HOME", ".cache"),
                     ("XDG_DATA_HOME", ".local/share")):
        path = os.path.join(home, sub)
        os.makedirs(path, exist_ok=True)
        env[var] = path
    return env


def _fetch_version() -> dict:
    with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2) as resp:
        return json.load(resp)


async def _wait_for_cdp(proc: asyncio.subprocess.Process) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STARTUP_TIMEOUT
    while True:
        if proc.returncode is not None:
            raise RuntimeError(f"Chromium exited early (code {proc.returncode})")
        try:
            return await asyncio.to_thread(_fetch_version)
        except (OSError, ValueError):
            if loop.time() >= deadline:
                raise RuntimeError(
                    f"{CDP_URL}/json/version not reachable within "
                    f"{STARTUP_TIMEOUT:.0f}s"
                ) from None
            await asyncio.sleep(0.25)


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=10)


async def selftest_cdp(chromium_bin: str) -> int:
    """Return 0 when Playwright drives the given Chromium over CDP, else 1."""
    if not (Path(chromium_bin).is_file() and os.access(chromium_bin, os.X_OK)):
        return _fail(f"chromium binary not found or not executable: {chromium_bin}")

    # Imported lazily: playwright exists only in the image's venv, not on dev hosts.
    try:
        from playwright.async_api import async_playwright
        from importlib.metadata import version as dist_version

        playwright_version = dist_version("playwright")
    except Exception as exc:
        return _fail(f"cannot import playwright: {exc}")

    tmp = tempfile.mkdtemp(prefix="stream-pilot-selftest-")
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            chromium_bin,
            *_chromium_args(os.path.join(tmp, "profile")),
            env=_chromium_env(os.path.join(tmp, "home")),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await _wait_for_cdp(proc)
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
            try:
                context = (
                    browser.contexts[0]
                    if browser.contexts
                    else await browser.new_context()
                )
                page = await context.new_page()
                result = await page.evaluate("1+1")
                if result != 2:
                    return _fail(f"page.evaluate('1+1') returned {result!r}, not 2")
                session = await browser.new_browser_cdp_session()
                info = await session.send("Browser.getVersion")
                product = info.get("product", "unknown")
            finally:
                with contextlib.suppress(Exception):
                    await browser.close()
        print(f"selftest-cdp: OK chromium={product} playwright={playwright_version}")
        return 0
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")
    finally:
        if proc is not None:
            await _stop(proc)
        shutil.rmtree(tmp, ignore_errors=True)
