"""stream-pilot command-line entry point.

Each subcommand dispatches to one small ``cmd_*`` function.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .app import run_app
from .chromium_prep import prep_chromium_profile
from .config import Config, load_config
from .health import check_health, format_status
from .seed_obs import reset_scenes, seed_obs
from .selftest import selftest_cdp

log = logging.getLogger("stream_pilot")

# Packaged default OBS profile/scenes (monkeypatched in tests).
OBS_DEFAULTS_DIR = Path("/usr/share/stream-pilot/obs")


def _config_paths() -> list[Path]:
    # Resolved at call time (not import time) so $HOME is honoured.
    return [
        Path("/etc/stream-pilot/config.toml"),
        Path("~/.config/stream-pilot/site.toml").expanduser(),
        Path("~/.config/stream-pilot/secrets.toml").expanduser(),
    ]


def _load_config() -> Config:
    return load_config(_config_paths())


def cmd_run(args: argparse.Namespace) -> int:
    """The long-running controller (stream-pilot.service)."""
    try:
        cfg = load_config(_config_paths())
    except Exception:
        log.exception("run: cannot load config")
        return 1
    return asyncio.run(run_app(cfg, args.dry_run))


def _status_path() -> Path:
    return _load_config().paths.state_dir / "status.json"


def cmd_status(_args: argparse.Namespace) -> int:
    path = _status_path()
    try:
        status = json.loads(path.read_text())
    except FileNotFoundError:
        print(f"no status file at {path} (is stream-pilot running?)", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"cannot read status file {path}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(status, dict):
        print(f"status file {path} is not a JSON object", file=sys.stderr)
        return 1
    print(format_status(status))
    return 0


def _run_command(argv: list[str]) -> int:
    """Run a command and return its exit code (monkeypatched in tests)."""
    return subprocess.run(argv, check=False).returncode


def cmd_retry(_args: argparse.Namespace) -> int:
    try:
        return _run_command(
            ["systemctl", "--user", "kill", "-s", "SIGUSR1", "stream-pilot.service"]
        )
    except OSError as exc:
        print(f"retry failed: {exc}", file=sys.stderr)
        return 1


def cmd_health(args: argparse.Namespace) -> int:
    # Run by greenboot: any failure here (even a config error) must be a
    # clean "unhealthy" exit code rather than a traceback.
    try:
        path = _status_path()
    except Exception as exc:
        print(f"stream-pilot health: UNHEALTHY: cannot load config: {exc}",
              file=sys.stderr)
        return 1
    return check_health(path, args.timeout)


def cmd_seed_obs(args: argparse.Namespace) -> int:
    """ExecStartPre for obs.service: must never block OBS except on a broken image.

    With ``--reset-scenes`` (run by hand, OBS stopped) also re-applies the
    camera/microphone IDs from the config to the scene collection.
    """
    try:
        cfg = _load_config()
        config_dir = Path("~/.config/obs-studio").expanduser()
        seed_obs(
            OBS_DEFAULTS_DIR,
            config_dir,
            Path("~/.config/stream-pilot/secrets.toml").expanduser(),
            cfg.cameras,
            audio=cfg.audio,
        )
        if getattr(args, "reset_scenes", False):
            stamp = reset_scenes(OBS_DEFAULTS_DIR, config_dir, cfg.cameras, cfg.audio)
            if stamp is None:
                print("reset-scenes: no existing scenes to back up")
            else:
                for backup in sorted(stamp.parent.glob(f"*.bak-{stamp.name}")):
                    print(f"reset-scenes: backed up {backup}")
            print("reset-scenes: scene collection re-applied from the defaults")
    except FileNotFoundError as exc:
        log.error("seed-obs: %s", exc)
        return 1
    except Exception:
        log.exception("seed-obs failed; continuing so OBS can still start")
    return 0


def cmd_chromium_prep(_args: argparse.Namespace) -> int:
    """ExecStartPre for chromium.service: never blocks the unit start."""
    try:
        cfg = _load_config()
        cfg.cdp.user_data_dir.mkdir(parents=True, exist_ok=True)
        prep_chromium_profile(cfg.cdp.user_data_dir)
    except Exception:
        log.exception("chromium-prep failed; continuing")
    return 0


def cmd_selftest_cdp(args: argparse.Namespace) -> int:
    """CI check: pinned Playwright must still attach to the image's Chromium."""
    chromium_bin = args.chromium_bin
    if chromium_bin is None:
        try:
            chromium_bin = _load_config().cdp.chromium_bin
        except Exception as exc:
            print(f"selftest-cdp: cannot load config: {exc}", file=sys.stderr)
            return 1
    return asyncio.run(selftest_cdp(chromium_bin))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stream-pilot",
        description="Controller for the Facebook-Group live-streaming PC.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    handlers: dict[str, tuple[Callable[[argparse.Namespace], int], str]] = {
        "run": (cmd_run, "run the controller daemon"),
        "status": (cmd_status, "print the current status"),
        "retry": (cmd_retry, "ask the running controller to retry"),
        "health": (cmd_health, "wait for the system to be healthy"),
        "seed-obs": (cmd_seed_obs, "seed the OBS configuration"),
        "chromium-prep": (cmd_chromium_prep, "clear Chromium crash flags"),
        "selftest-cdp": (
            cmd_selftest_cdp,
            "check that Playwright can attach to the system Chromium over CDP",
        ),
    }
    parsers = {}
    for name, (func, help_text) in handlers.items():
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        parsers[name] = p

    parsers["run"].add_argument(
        "--dry-run",
        action="store_true",
        help="do everything up to, but not including, clicking Go live",
    )
    parsers["seed-obs"].add_argument(
        "--reset-scenes",
        action="store_true",
        help="re-apply camera/microphone IDs from site.toml (backs up the "
        "current scene collection); stop OBS first",
    )
    parsers["selftest-cdp"].add_argument(
        "--chromium-bin",
        default=None,
        metavar="PATH",
        help="Chromium executable (default: cdp.chromium_bin from the config)",
    )
    parsers["health"].add_argument(
        "--timeout",
        type=float,
        default=300,
        metavar="SECONDS",
        help="seconds to wait for healthy state (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
