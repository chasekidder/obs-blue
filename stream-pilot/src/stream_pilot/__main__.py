"""stream-pilot command-line entry point.

Each subcommand dispatches to one small ``cmd_*`` function. Later tasks
replace those handlers; until then they report ``not implemented`` and
return exit code 2.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable


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


def cmd_seed_obs(args: argparse.Namespace) -> int:
    return _not_implemented(args)


def cmd_chromium_prep(args: argparse.Namespace) -> int:
    return _not_implemented(args)


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
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
