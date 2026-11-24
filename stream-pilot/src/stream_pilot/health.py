"""Helpers for the ``status`` and ``health`` subcommands.

``check_health`` is what the greenboot boot health check runs; its exit code
decides whether a newly applied OS image is rolled back, so it must only
return 0 when the controller is demonstrably alive.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

# status.json older than this means the controller stopped updating it.
FRESH_WINDOW_S = 180
# Seconds between checks while waiting for the controller to come up.
POLL_INTERVAL_S = 5


def _fmt_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def format_status(status: dict, now: float | None = None) -> str:
    """Render a status.json dict for humans (robust to missing keys)."""
    if now is None:
        now = time.time()

    state = status.get("state") or "?"
    since = status.get("since")
    state_line = f"state: {state}"
    if _is_number(since):
        state_line += f" (since {_fmt_duration(now - since)})"

    updated = status.get("updated_at")
    updated_line = (
        f"updated: {_fmt_duration(now - updated)} ago" if _is_number(updated)
        else "updated: ?"
    )

    memory = status.get("memory")
    avail = memory.get("mem_available_mb") if isinstance(memory, dict) else None
    mem_line = f"mem available: {avail} MB" if _is_number(avail) else "mem available: -"

    lines = [
        state_line,
        updated_line,
        f"last error: {status.get('last_error') or '-'}",
        f"live id: {status.get('live_id') or '-'}",
        mem_line,
    ]
    features = status.get("features")
    if isinstance(features, dict):
        lines.append("features:")
        lines.extend(
            f"  {name}: {'on' if on is True else 'off'}" for name, on in features.items()
        )
    return "\n".join(lines)


def _unhealthy_reason(status_path: Path, now: float) -> str | None:
    """Return why the system is not healthy yet, or None if it is healthy."""
    try:
        raw = status_path.read_text()
    except FileNotFoundError:
        return f"status file missing: {status_path}"
    except OSError as exc:
        return f"status file unreadable: {status_path}: {exc}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return f"status file unreadable (corrupt JSON): {exc}"
    if not isinstance(data, dict):
        return "status file unreadable (not a JSON object)"
    state = data.get("state")
    if not isinstance(state, str):
        return "status file has no valid state"
    if state == "STARTING":
        return "state is still STARTING"
    updated = data.get("updated_at")
    if not _is_number(updated):
        return "status file has no valid updated_at"
    age = now - updated
    if age > FRESH_WINDOW_S:
        return f"status is stale (updated {int(age)}s ago, limit {FRESH_WINDOW_S}s)"
    return None


def check_health(
    status_path: Path,
    timeout_s: float,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll until the controller reports healthy; 0 if so, else 1 at timeout.

    Healthy = status file exists and parses, ``state != "STARTING"`` and
    ``updated_at`` is within ``FRESH_WINDOW_S``. Always checks at least once
    (including at t=0). On failure prints a one-line reason to stderr so the
    greenboot journal shows why.
    """
    status_path = Path(status_path)
    deadline = now() + timeout_s
    while True:
        reason = _unhealthy_reason(status_path, now())
        if reason is None:
            return 0
        remaining = deadline - now()
        if remaining <= 0:
            print(f"stream-pilot health: UNHEALTHY: {reason}", file=sys.stderr)
            return 1
        sleep(min(POLL_INTERVAL_S, remaining))
