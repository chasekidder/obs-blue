"""Clear Chromium's crash flags so it never offers to "Restore pages?", and its stale profile lock."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ._atomic import atomic_write_text

log = logging.getLogger(__name__)

_SINGLETON_FILES = ("SingletonCookie", "SingletonLock", "SingletonSocket")


def clear_singleton_lock(user_data_dir: Path) -> list[str]:
    """Remove the profile lock links a previous Chromium left behind; return their names.

    Only safe before chromium.service starts the browser (its ExecStartPre), when
    nothing can hold the profile. Chromium clears a dead lock itself only if the
    hostname in it matches, and the hostname changes early in boot (``fedora``
    until DHCP sets ``stream-pc.lan``), so a stale lock can block the profile with
    "in use by another Chromium process on another computer" and no CDP port.
    """
    removed = []
    for name in _SINGLETON_FILES:
        path = Path(user_data_dir) / name
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("cannot remove stale Chromium %s: %s", path, exc)
            continue
        removed.append(name)
    if removed:
        log.info("removed stale Chromium profile lock: %s", ", ".join(removed))
    return removed


def prep_chromium_profile(user_data_dir: Path) -> bool:
    """Set ``profile.exit_type = "Normal"`` and ``profile.exited_cleanly = True``.

    Returns True only if the Preferences file was changed. A missing or
    corrupt file is tolerated (logged, returns False); this never raises for
    bad profile content.
    """
    prefs = Path(user_data_dir) / "Default" / "Preferences"
    try:
        raw = prefs.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("cannot read Chromium Preferences %s: %s", prefs, exc)
        return False
    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.warning("corrupt Chromium Preferences %s: %s", prefs, exc)
        return False
    if not isinstance(data, dict):
        log.warning("unexpected Chromium Preferences structure in %s", prefs)
        return False

    profile = data.get("profile")
    if not isinstance(profile, dict):
        profile = {}
        data["profile"] = profile
    changed = False
    for key, value in (("exit_type", "Normal"), ("exited_cleanly", True)):
        if profile.get(key) != value or type(profile.get(key)) is not type(value):
            profile[key] = value
            changed = True
    if not changed:
        return False

    try:
        mode = prefs.stat().st_mode & 0o777
    except OSError:
        mode = 0o600
    try:
        atomic_write_text(prefs, json.dumps(data, separators=(",", ":")), mode)
    except OSError as exc:
        log.warning("cannot write Chromium Preferences %s: %s", prefs, exc)
        return False
    return True
