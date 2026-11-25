"""``[upload]``: copy the stream folders off the PC with rsync over SSH.

``stream-pilot upload`` (``stream-pilot-upload.service``, started about every
60 s by ``stream-pilot-upload.timer`` while the switch is on) copies, at low
CPU and I/O priority and at most ``bwlimit_kbps``:

* ``status.json`` to ``<target>/_status/`` as a heartbeat, while stream-pilot
  is running (the file is fresh);
* every stream folder not yet fully copied, the current live's first, to
  ``<target>/<folder name>/``: the finished files (``stream.json``,
  ``report.txt``, ``journal.txt``, ``audio/manifest.jsonl`` and the clips it
  lists), ``comments.jsonl`` appended (it only grows; a partial last line is
  completed next time), and ``DONE`` last, once everything else is there.
  A folder whose ``DONE`` was copied gets a local ``.uploaded`` marker;
* new failure snapshots from ``failures_dir`` to ``<target>/_failures/``;

and then deletes local stream folders that are fully copied and older than
``retention_days``.

SSH never prompts: ``BatchMode=yes``, the key generated on first use under
``~/.config/stream-pilot/``, and the server's host key pinned from config (no
other known_hosts file is read). One run at a time (a lock file).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

from . import stream_meta
from .config import Config, UploadCfg
from .features import Feature
from .health import FRESH_WINDOW_S
from .recorder import MANIFEST, Manifest, clips

log = logging.getLogger(__name__)

CONFIG_DIR = Path("~/.config/stream-pilot")
KEY_NAME = "upload_ed25519"
KNOWN_HOSTS_NAME = "upload_known_hosts"
UPLOADED = ".uploaded"
JOURNAL = "journal.txt"
STATUS_DIR = "_status"
FAILURES_DIR = "_failures"
SERVICE = "stream-pilot-upload.service"
TIMER = "stream-pilot-upload.timer"
SERVER_ALIVE_S = 5
# One timer run copies for at most this long (a big backlog continues next run, from
# rsync's partial files), so the live's folder is refreshed about every minute.
RUN_BUDGET_S = 45.0
_TERM_WAIT_S = 3.0
_FINISHED = ("stream.json", "report.txt", JOURNAL)

Runner = Callable[[list[str]], Awaitable[int]]


def config_dir() -> Path:
    return CONFIG_DIR.expanduser()


def parse_target(target: str) -> tuple[str, str, str] | None:
    """``user@host:path`` -> (``user@host``, ``host``, ``path``); None if malformed."""
    login, sep, path = target.strip().partition(":")
    if not sep or not login or not path:
        return None
    host = login.rpartition("@")[2]
    return (login, host, path) if host else None


def bwlimit_kib(kbps: int) -> int:
    """rsync's --bwlimit is in KiB/s; the config is in kilobits per second."""
    return max(1, round(kbps * 1000 / 8 / 1024))


def known_hosts_line(host: str, port: int, host_key: str) -> str:
    name = host if port == 22 else f"[{host}]:{port}"
    return f"{name} {host_key.strip()}\n"


def ensure_key(directory: Path | None = None) -> Path:
    """The upload key pair (ed25519, no passphrase), generated on first use."""
    directory = directory or config_dir()
    key = directory / KEY_NAME
    if key.exists() and key.with_suffix(".pub").exists():
        return key
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (key, key.with_suffix(".pub")):
        path.unlink(missing_ok=True)
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
         f"stream-pilot@{socket.gethostname()}", "-f", str(key)],
        check=True, stdin=subprocess.DEVNULL,
    )
    key.chmod(0o600)
    return key


def authorized_keys_line(pubkey: str, inbox: str = "/srv/stream-pc/inbox") -> str:
    """The line to put in the server's authorized_keys: rrsync, write-only, one directory."""
    return f'restrict,command="rrsync -wo {inbox}" {pubkey.strip()}'


def finished_files(stream_dir: Path) -> list[str]:
    """Files of a stream folder that will not change any more (relative paths)."""
    out = [name for name in _FINISHED if (stream_dir / name).is_file()]
    manifest = stream_dir / "audio" / MANIFEST
    if manifest.is_file():
        out.append(f"audio/{MANIFEST}")
        out.extend(f"audio/{name}" for name in clips(Manifest(manifest).records())
                   if (stream_dir / "audio" / name).is_file())
    return out


def stream_folders(streams_dir: Path) -> list[Path]:
    """Folders not fully copied yet: the ones still being written first, then newest first."""
    try:
        dirs = [d for d in streams_dir.iterdir() if d.is_dir() and not (d / UPLOADED).exists()]
    except FileNotFoundError:
        return []
    dirs.sort(key=lambda d: d.name, reverse=True)
    return [d for d in dirs if not (d / stream_meta.DONE).exists()] + [
        d for d in dirs if (d / stream_meta.DONE).exists()
    ]


async def run_rsync(argv: list[str]) -> int:
    """Run rsync; killed if cancelled (the end flow's time budget)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        _, err = await proc.communicate()
    except BaseException:
        if proc.returncode is None:
            # SIGTERM first: rsync then keeps the partial file (--partial) for the next run.
            proc.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), _TERM_WAIT_S)
            except BaseException:
                proc.kill()
                await proc.wait()
        raise
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip().splitlines()[-3:]
        log.warning("upload: rsync exited with %s: %s", proc.returncode, " | ".join(tail))
    return proc.returncode


@contextmanager
def run_lock(state_dir: Path) -> Iterator[bool]:
    """Non-blocking lock so two uploads never write the same files; yields False if busy."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "upload.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Uploader:
    def __init__(
        self,
        cfg: Config,
        *,
        run: Runner = run_rsync,
        keys_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.up: UploadCfg = cfg.upload
        self._run = run
        self._keys_dir = keys_dir or config_dir()
        self._clock = clock
        self._target = self.up.target.strip().rstrip("/")
        self._local = ":" not in self._target  # a plain directory (tests, a mounted disk)

    # -- setup -------------------------------------------------------------

    def problem(self) -> str | None:
        """Why uploads cannot run with this config (None: they can)."""
        if not self._target:
            return "no [upload] target configured"
        if self._local:
            return None
        if parse_target(self._target) is None:
            return f"[upload] target must look like user@host:path, not {self._target!r}"
        if not self.up.host_key.strip():
            return "no [upload] host_key configured (the server's key is pinned)"
        return None

    def _ssh(self) -> str:
        _, host, _ = parse_target(self._target)  # type: ignore[misc]
        key = ensure_key(self._keys_dir)
        known = self._keys_dir / KNOWN_HOSTS_NAME
        line = known_hosts_line(host, self.up.port, self.up.host_key)
        if not known.exists() or known.read_text() != line:
            known.write_text(line)
        control = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        return shlex.join([
            "ssh", "-p", str(self.up.port), "-i", str(key),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={self.up.connect_timeout_s}",
            "-o", f"ServerAliveInterval={SERVER_ALIVE_S}", "-o", "ServerAliveCountMax=3",
            "-o", "ControlMaster=auto", "-o", f"ControlPath={control}/stream-pilot-upload-%C",
            "-o", "ControlPersist=60",
        ])

    def _rsync(self, *args: str) -> list[str]:
        argv = ["rsync", "-rt", "--partial", f"--timeout={self.up.timeout_s}",
                f"--bwlimit={bwlimit_kib(self.up.bwlimit_kbps)}", "--mkpath"]
        if not self._local:
            argv += ["-e", self._ssh()]
        return [*argv, *args]

    def _dest(self, *parts: str) -> str:
        return "/".join([self._target, *parts]) + "/"

    # -- uploads -----------------------------------------------------------

    async def upload_stream(self, stream_dir: Path) -> bool:
        """Copy one stream folder; True once it is fully copied (DONE included)."""
        stream_dir = Path(stream_dir)
        dest = self._dest(stream_dir.name)
        ok = True
        files = finished_files(stream_dir)
        if files:
            with tempfile.NamedTemporaryFile("w", prefix="stream-pilot-upload-", suffix=".list") as lst:
                lst.write("\n".join(files) + "\n")
                lst.flush()
                ok = await self._run(self._rsync(f"--files-from={lst.name}",
                                                 f"{stream_dir}/", dest)) == 0
        comments = stream_dir / "comments.jsonl"
        if comments.is_file():
            ok = await self._run(self._rsync("--append", str(comments), dest)) == 0 and ok
        done = stream_dir / stream_meta.DONE
        if not ok or not done.exists():
            return False
        if await self._run(self._rsync(str(done), dest)) != 0:
            return False
        (stream_dir / UPLOADED).write_text(f"{self._clock():.3f}\n")
        log.info("upload: %s fully copied", stream_dir.name)
        return True

    async def heartbeat(self) -> bool:
        status = self.cfg.paths.state_dir / "status.json"
        try:
            updated = json.loads(status.read_text()).get("updated_at")
        except (OSError, ValueError, AttributeError):
            return True
        if not isinstance(updated, (int, float)) or self._clock() - updated > FRESH_WINDOW_S:
            return True  # stream-pilot is not running: nothing to report
        return await self._run(self._rsync(str(status), self._dest(STATUS_DIR))) == 0

    async def failures(self) -> bool:
        src = self.cfg.paths.failures_dir
        if not src.is_dir() or not any(src.iterdir()):
            return True
        return await self._run(self._rsync("--exclude=*.tmp", f"{src}/",
                                           self._dest(FAILURES_DIR))) == 0

    def retention(self) -> list[Path]:
        """Delete fully copied stream folders older than retention_days."""
        removed = []
        limit = self.up.retention_days * 86400
        try:
            dirs = [d for d in self.cfg.paths.streams_dir.iterdir() if d.is_dir()]
        except FileNotFoundError:
            return removed
        for d in dirs:
            marker, done = d / UPLOADED, d / stream_meta.DONE
            if not (marker.exists() and done.exists()):
                continue
            if self._clock() - done.stat().st_mtime <= limit:
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
            log.info("upload: deleted %s (copied, older than %g days)", d.name,
                     self.up.retention_days)
        return removed

    async def run_all(self, budget_s: float | None = None) -> int:
        """One timer run, at most ``budget_s`` (RUN_BUDGET_S). Returns 0: problems are
        logged and the timer simply tries again."""
        problem = self.problem()
        if problem:
            log.warning("upload: %s; nothing copied", problem)
            return 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (RUN_BUDGET_S if budget_s is None else budget_s)
        steps: list[Callable[[], Awaitable[object]]] = [self.heartbeat]
        steps += [lambda d=d: self.upload_stream(d)
                  for d in stream_folders(self.cfg.paths.streams_dir)]
        steps.append(self.failures)
        for step in steps:
            left = deadline - loop.time()
            if left <= 0:
                log.info("upload: this run's time is used up; the rest follows next run")
                break
            try:
                await asyncio.wait_for(step(), left)
            except TimeoutError:
                log.info("upload: this run's time is used up; the rest follows next run")
                break
        self.retention()
        return 0


def journal_since(stream_dir: Path) -> float | None:
    meta = stream_meta.read(stream_dir)
    for key in ("preroll_started_at", "go_live_pressed_at", "live_confirmed_at"):
        if isinstance(meta.get(key), (int, float)):
            return float(meta[key])
    try:
        return time.mktime(time.strptime(Path(stream_dir).name[:15], "%Y-%m-%d_%H%M"))
    except ValueError:
        return None


async def write_journal(
    stream_dir: Path, exec_fn: Callable[..., Any] = asyncio.create_subprocess_exec,
    timeout: float = 10.0,
) -> bool:
    """``journal.txt``: stream-pilot's log since the stream started."""
    since = journal_since(stream_dir)
    argv = ["journalctl", "--user", "-u", "stream-pilot.service", "--no-pager", "-o", "short-iso"]
    if since is not None:
        argv += ["--since", f"@{int(since)}"]
    proc = await exec_fn(*argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        raise
    path = Path(stream_dir) / JOURNAL
    tmp = path.with_name(JOURNAL + ".tmp")
    tmp.write_bytes(out)
    tmp.replace(path)
    return proc.returncode == 0


class Upload(Feature):
    """The switch: the timer runs only while it is on; the end flow adds the final copy."""

    name = "upload"

    def __init__(
        self, cfg: Config, *, system: Any, uploader: Uploader | None = None,
        exec_fn: Callable[..., Any] = asyncio.create_subprocess_exec,
    ):
        super().__init__(cfg.upload.enabled)
        self.cfg = cfg
        self.system = system
        self.uploader = uploader or Uploader(cfg)
        self._exec = exec_fn

    async def restore(self) -> None:
        await self.system.stop_user_unit(TIMER)
        await self.system.stop_user_unit(SERVICE)

    async def started(self) -> None:
        problem = self.uploader.problem()
        if problem:
            log.warning("upload: %s; uploads stay off until it is fixed", problem)
        if not await self.system.start_user_unit(TIMER):
            log.warning("upload: cannot start %s", TIMER)

    async def stream_closed(self, stream_dir: Path) -> None:
        await write_journal(stream_dir, self._exec)

    async def stream_done(self, stream_dir: Path) -> None:
        """The final copy before power-off (the caller bounds it by final_timeout_s)."""
        done = Path(stream_dir) / stream_meta.DONE
        if not done.exists():
            stream_meta.write_done(stream_dir, time.time())
        if self.uploader.problem():
            return
        try:
            await asyncio.wait_for(self._final(Path(stream_dir)), self.cfg.upload.final_timeout_s)
        except TimeoutError:
            log.warning("upload: the final copy did not finish in %gs; the next boot catches up",
                        self.cfg.upload.final_timeout_s)

    async def _final(self, stream_dir: Path) -> None:
        await self.system.stop_user_unit(SERVICE)  # a timer run would hold the lock
        with run_lock(self.cfg.paths.state_dir) as locked:
            if not locked:
                log.warning("upload: another upload is running; the final copy is left to it")
                return
            await self.uploader.heartbeat()
            await self.uploader.upload_stream(stream_dir)
