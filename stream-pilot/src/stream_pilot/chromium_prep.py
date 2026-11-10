"""Clear Chromium's crash flags so it never offers to "Restore pages?"."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ._atomic import atomic_write_text

log = logging.getLogger(__name__)


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
