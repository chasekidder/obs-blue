"""stream-pilot command-line entry point.

Each subcommand dispatches to one small ``cmd_*`` function. Later tasks
replace those handlers; until then they report ``not implemented`` and
return exit code 2.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from pathlib import Path

from .chromium_prep import prep_chromium_profile
from .config import Config, load_config
from .seed_obs import seed_obs

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


def _not_implemented(_args: argparse.Namespace) -> int:
    print("not implemented")
    return 2


def cmd_run(args: argparse.Namespace) -> int:
    return _not_implemented(args)


def cmd_status(args: argparse.Namespace) -> int:
    return _not_implemented(args)


def cmd_retry(args: argparse.Namespace) -> int:
    return _not_implemented(args)


def cmd_health(args: argparse.Namespace) -> int:
    return _not_implemented(args)


def cmd_seed_obs(_args: argparse.Namespace) -> int:
    """ExecStartPre for obs.service: must never block OBS except on a broken image."""
    try:
        cfg = _load_config()
        seed_obs(
            OBS_DEFAULTS_DIR,
            Path("~/.config/obs-studio").expanduser(),
            Path("~/.config/stream-pilot/secrets.toml").expanduser(),
            cfg.cameras,
        )
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
    }
    parsers = {}
    for name, (func, help_text) in handlers.items():
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        parsers[name] = p

    parsers["run"].add_argument(
        "--dry-run",
        action="store_true",
        help="do not touch real devices or OBS; log intended actions",
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
