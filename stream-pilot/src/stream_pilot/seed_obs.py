"""Seed the OBS configuration and (re)write the obs-websocket config.

Runs as ``obs.service`` ExecStartPre on every OBS start:

* first run (no ``config_dir``): copy the packaged default profile/scenes and
  substitute the camera/microphone device placeholders;
* every run: make sure ``secrets.toml`` holds ``[obs] password`` (generated
  once, mode 0600) and write obs-websocket's ``config.json`` with it.
* :func:`reset_scenes` (``seed-obs --reset-scenes``) re-applies the device IDs
  from the current config to an already-seeded scene collection.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from ._atomic import atomic_write_text
from .config import AudioCfg, CameraCfg

log = logging.getLogger(__name__)

MAIN_PLACEHOLDER = "@MAIN_CAMERA_DEVICE@"
JEWELRY_PLACEHOLDER = "@JEWELRY_CAMERA_DEVICE@"
MICROPHONE_PLACEHOLDER = "@MICROPHONE_DEVICE@"
WEBSOCKET_PORT = 4455


def clear_stale_sentinels(config_dir: Path) -> list[str]:
    """Remove the ``.sentinel/run_*`` files earlier OBS sessions left; return their names.

    Only safe before obs.service starts OBS (its ExecStartPre), when no OBS is
    running. OBS deletes its sentinel only on an orderly exit, and on start any
    leftover one makes OBS 32 wait on a modal "OBS Studio Crash Detected" dialog
    (``--disable-shutdown-check`` does not suppress it), so obs-websocket never
    starts. An OBS killed in that dialog leaves another sentinel, so after one
    power cut every later boot would stop there.
    """
    removed = []
    for path in sorted((Path(config_dir) / ".sentinel").glob("run_*")):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("cannot remove stale OBS sentinel %s: %s", path, exc)
            continue
        removed.append(path.name)
    if removed:
        log.info("removed stale OBS crash sentinel: %s", ", ".join(removed))
    return removed


def seed_obs(
    defaults_dir: Path,
    config_dir: Path,
    secrets_path: Path,
    cameras: dict[str, CameraCfg],
    audio: AudioCfg | None = None,
) -> bool:
    """Seed OBS; return True if the defaults were copied this run.

    ``audio`` supplies the microphone device; if ``None`` the
    ``@MICROPHONE_DEVICE@`` placeholder is left as is.

    Raises ``FileNotFoundError`` if the defaults are needed (``config_dir``
    does not exist yet) but ``defaults_dir`` is missing.
    """
    defaults_dir, config_dir, secrets_path = (
        Path(defaults_dir),
        Path(config_dir),
        Path(secrets_path),
    )
    copied = False
    if not config_dir.exists():
        _copy_defaults(defaults_dir, config_dir, cameras, audio)
        copied = True

    password = _ensure_password(secrets_path)
    _write_websocket_config(config_dir, password)
    return copied


def _replacements(
    cameras: dict[str, CameraCfg], audio: AudioCfg | None
) -> dict[str, str]:
    out = {
        MAIN_PLACEHOLDER: _json_escape(cameras["main"].device),
        JEWELRY_PLACEHOLDER: _json_escape(cameras["jewelry"].device),
    }
    if audio is not None:
        out[MICROPHONE_PLACEHOLDER] = _json_escape(audio.mic_device)
    return out


def _check_defaults(defaults_dir: Path) -> None:
    if not defaults_dir.is_dir():
        raise FileNotFoundError(f"OBS defaults directory not found: {defaults_dir}")


def reset_scenes(
    defaults_dir: Path,
    config_dir: Path,
    cameras: dict[str, CameraCfg],
    audio: AudioCfg | None,
    *,
    now: datetime | None = None,
) -> Path | None:
    """Re-apply the packaged scene collection with the current device IDs.

    Every existing ``config_dir/basic/scenes/*.json`` is first copied to
    ``<name>.bak-<YYYYmmdd-HHMMSS>`` (same directory; a ``-N`` suffix is added
    to the stamp if that backup name is already taken), then the defaults'
    scenes are copied over with all placeholders substituted. Nothing else in
    ``config_dir`` is touched. OBS should be stopped first: it rewrites its
    scene files on exit.

    Returns ``<scenes dir>/<stamp>`` (its ``name`` is the backup stamp, i.e.
    the files are ``*.bak-<stamp>``), or ``None`` if there was nothing to back
    up. Raises ``FileNotFoundError`` (before touching anything) if
    ``defaults_dir`` is missing.
    """
    defaults_dir, config_dir = Path(defaults_dir), Path(config_dir)
    _check_defaults(defaults_dir)
    replacements = _replacements(cameras, audio)
    scenes_dir = config_dir / "basic" / "scenes"

    existing = sorted(scenes_dir.glob("*.json")) if scenes_dir.is_dir() else []
    stamp = None
    if existing:
        base = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        stamp, n = base, 0
        while any(f.with_name(f"{f.name}.bak-{stamp}").exists() for f in existing):
            n += 1
            stamp = f"{base}-{n}"
        for f in existing:
            shutil.copy2(f, f.with_name(f"{f.name}.bak-{stamp}"))

    scenes_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted((defaults_dir / "basic" / "scenes").glob("*.json")):
        text = src.read_text(encoding="utf-8")
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)
        atomic_write_text(scenes_dir / src.name, text, 0o644)
    return scenes_dir / stamp if stamp else None


def _copy_defaults(
    defaults_dir: Path,
    config_dir: Path,
    cameras: dict[str, CameraCfg],
    audio: AudioCfg | None,
) -> None:
    _check_defaults(defaults_dir)
    replacements = _replacements(cameras, audio)
    config_dir.parent.mkdir(parents=True, exist_ok=True)
    # Build in a sibling temp dir and rename, so an interrupted copy never
    # leaves a half-seeded config_dir that would be mistaken for a user's.
    staging = Path(tempfile.mkdtemp(dir=config_dir.parent, prefix=f".{config_dir.name}."))
    try:
        target = staging / "tree"
        shutil.copytree(defaults_dir, target)
        for scene in sorted((target / "basic" / "scenes").glob("*.json")):
            text = scene.read_text(encoding="utf-8")
            for placeholder, value in replacements.items():
                text = text.replace(placeholder, value)
            scene.write_text(text, encoding="utf-8")
        os.rename(target, config_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _json_escape(value: str) -> str:
    return json.dumps(value)[1:-1]


# --- secrets.toml ----------------------------------------------------------


def _read_secrets(path: Path) -> tuple[dict[str, Any], bool]:
    """Return (data, existed_and_valid)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, False
    try:
        return tomllib.loads(raw.decode("utf-8")), True
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        backup = path.with_name(path.name + ".corrupt")
        log.warning(
            "corrupt secrets file %s (%s); starting fresh, old copy kept as %s",
            path, exc, backup,
        )
        try:
            atomic_write_text(backup, raw.decode("utf-8", "replace"), 0o600)
        except OSError as bexc:
            log.warning("could not back up corrupt secrets file: %s", bexc)
        return {}, False


def _ensure_password(path: Path) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data, valid = _read_secrets(path)
    obs = data.get("obs")
    pw = obs.get("password") if isinstance(obs, dict) else None
    if isinstance(pw, str) and pw:
        if valid and (path.stat().st_mode & 0o777) != 0o600:
            os.chmod(path, 0o600)
        return pw
    if not isinstance(obs, dict):
        obs = data["obs"] = {}
    pw = secrets.token_urlsafe(24)
    obs["password"] = pw
    atomic_write_text(path, dumps_toml(data), 0o600)
    return pw


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n",
            "\f": "\\f", "\r": "\\r"}


def _quote(s: str) -> str:
    out = []
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else _quote(k)


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v:
            return "nan"
        if v in (float("inf"), float("-inf")):
            return "inf" if v > 0 else "-inf"
        return repr(v)
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, list):
        return "[" + ", ".join(_scalar(x) for x in v) + "]"
    raise TypeError(f"cannot serialize {type(v).__name__} to TOML")


def dumps_toml(data: dict[str, Any]) -> str:
    """Serialize nested tables of str/int/float/bool (and scalar lists)."""
    lines: list[str] = []

    def emit(table: dict[str, Any], path: list[str]) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in table.items() if isinstance(v, dict)}
        if path and (scalars or not tables):
            if lines:
                lines.append("")
            lines.append("[" + ".".join(_key(p) for p in path) + "]")
        for k, v in scalars.items():
            lines.append(f"{_key(k)} = {_scalar(v)}")
        for k, v in tables.items():
            emit(v, [*path, k])

    emit(data, [])
    return "\n".join(lines) + "\n"


# --- obs-websocket ---------------------------------------------------------


def _write_websocket_config(config_dir: Path, password: str) -> None:
    ws_dir = config_dir / "plugin_config" / "obs-websocket"
    ws_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "alerts_enabled": False,
        "auth_required": True,
        "first_load": False,
        "server_enabled": True,
        "server_password": password,
        "server_port": WEBSOCKET_PORT,
    }
    atomic_write_text(ws_dir / "config.json", json.dumps(payload, indent=2) + "\n", 0o600)
