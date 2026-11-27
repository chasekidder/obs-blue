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
from .chromium_prep import clear_singleton_lock, prep_chromium_profile
from .config import Config, load_config
from .display import run_display_mirror
from .health import check_health, format_status
from .power_guard import run_power_guard
from .seed_obs import clear_stale_sentinels, reset_scenes, seed_obs
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
    record_dir = None
    if getattr(args, "record", None):
        record_dir = Path(args.record).expanduser()
        try:
            # Captures may contain the stream key: owner-only, like fb/producer.py's files.
            record_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            record_dir.chmod(0o700)
        except OSError:
            log.exception("run: cannot create the --record directory %s", record_dir)
            return 1
    return asyncio.run(run_app(cfg, args.dry_run, record_dir=record_dir))


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
            ["systemctl", "--user", "kill", "--kill-whom=main", "-s", "SIGUSR1",
             "stream-pilot.service"]
        )
    except OSError as exc:
        print(f"retry failed: {exc}", file=sys.stderr)
        return 1


def cmd_press(args: argparse.Namespace) -> int:
    sig = "SIGRTMIN" if args.long else "SIGUSR2"
    try:
        return _run_command(
            ["systemctl", "--user", "kill", "--kill-whom=main", "-s", sig,
             "stream-pilot.service"]
        )
    except OSError as exc:
        print(f"press failed: {exc}", file=sys.stderr)
        return 1


def cmd_pause(_args: argparse.Namespace) -> int:
    """Pause stream-pilot (it changes nothing until resumed), or resume it."""
    try:
        return _run_command(
            ["systemctl", "--user", "kill", "--kill-whom=main", "-s", "SIGRTMIN+1",
             "stream-pilot.service"]
        )
    except OSError as exc:
        print(f"pause failed: {exc}", file=sys.stderr)
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
    config_dir = Path("~/.config/obs-studio").expanduser()
    try:
        clear_stale_sentinels(config_dir)
    except Exception:
        log.exception("clearing stale OBS sentinels failed; continuing")
    try:
        cfg = _load_config()
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
    _ensure_upload_key()
    return 0


def _ensure_upload_key() -> None:
    """First boot: the [upload] SSH key, like the websocket password (never blocks OBS)."""
    from .upload import ensure_key

    try:
        ensure_key()
    except Exception as exc:
        log.warning("seed-obs: cannot create the upload SSH key: %s", exc)


def cmd_chromium_prep(_args: argparse.Namespace) -> int:
    """ExecStartPre for chromium.service: never blocks the unit start."""
    try:
        cfg = _load_config()
        cfg.cdp.user_data_dir.mkdir(parents=True, exist_ok=True)
        clear_singleton_lock(cfg.cdp.user_data_dir)
        prep_chromium_profile(cfg.cdp.user_data_dir)
    except Exception:
        log.exception("chromium-prep failed; continuing")
    return 0


def cmd_power_guard(args: argparse.Namespace) -> int:
    """stream-pilot-power-guard.service (root): make every power-button press end in a power-off."""
    return run_power_guard(args.user)


def cmd_display_mirror(_args: argparse.Namespace) -> int:
    """stream-pilot-display-mirror.service (user): every connected display mirrors the primary."""
    return run_display_mirror()


def cmd_record(args: argparse.Namespace) -> int:
    """stream-pilot-recorder@.service (user): record the microphone into <stream_dir>/audio/."""
    from .recorder import run_recorder

    try:
        cfg = _load_config()
    except Exception:
        log.exception("record: cannot load config")
        return 1
    return run_recorder(Path(args.stream_dir), cfg.audio, cfg.recording)


def cmd_diag(args: argparse.Namespace) -> int:
    """stream-pilot-diag@.service (user): measurements of a live into <stream_dir>/diag/."""
    from .diagnostics import run_collector

    try:
        cfg = _load_config()
    except Exception:
        log.exception("diag: cannot load config")
        return 1
    return run_collector(Path(args.stream_dir), cfg)


def cmd_upload(_args: argparse.Namespace) -> int:
    """stream-pilot-upload.service (user, from its timer): copy the stream folders off the PC."""
    from .upload import Uploader, run_lock

    try:
        cfg = _load_config()
    except Exception:
        log.exception("upload: cannot load config")
        return 1
    if not cfg.upload.enabled:
        print("upload: [upload] enabled is false; nothing to do")
        return 0
    with run_lock(cfg.paths.state_dir) as locked:
        if not locked:
            print("upload: another upload is still running")
            return 0
        return asyncio.run(Uploader(cfg).run_all())


def cmd_upload_key(_args: argparse.Namespace) -> int:
    """Print the upload SSH public key and a matching restricted authorized_keys line."""
    from .upload import authorized_keys_line, ensure_key, parse_target

    try:
        pub = ensure_key().with_suffix(".pub").read_text().strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"upload-key: cannot create the key: {exc}", file=sys.stderr)
        return 1
    try:
        target = _load_config().upload.target
    except Exception:
        target = ""
    parsed = parse_target(target) if target else None
    print(pub)
    print()
    print("On the server, add this line to the receiving account's ~/.ssh/authorized_keys")
    print("(rrsync, write-only, limited to one inbox directory; adjust the directory):")
    print()
    print(authorized_keys_line(pub))
    print()
    if parsed:
        login, host, path = parsed
        print(f"[upload] target is {target}: with rrsync, {path!r} is relative to that directory.")
        print(f"Pin the server's host key in site.toml: [upload] host_key = \"<output of "
              f"ssh-keyscan -t ed25519 {host}, without the host name>\"")
    else:
        print('Then set [upload] target = "user@host:path" and host_key in site.toml.')
    return 0


def cmd_rebalance(args: argparse.Namespace) -> int:
    """Re-balance a frozen camera's white balance: auto for rebalance_s, then frozen again."""
    from .camera_ctl import V4l2Controls
    from .camera_features import WbFreeze

    cfg = _load_config()
    name = args.camera or cfg.wb_freeze.camera
    cam = cfg.cameras.get(name)
    if cam is None:
        print(f"rebalance: no camera {name!r} in the config", file=sys.stderr)
        return 1

    async def run() -> int:
        ctl = V4l2Controls(cam.device)
        names = await ctl.names()
        if names.wb_auto is None:
            print(f"rebalance: {cam.device} has no white balance controls", file=sys.stderr)
            return 1
        if await ctl.get(names.wb_auto) == 1:
            print(f"rebalance: {name} is on auto white balance; nothing to re-balance")
            return 0
        await WbFreeze(cfg, obs=None, controls=ctl).rebalance()
        print(f"rebalance: {name} re-balanced and frozen at "
              f"{await ctl.get(names.wb_temp) if names.wb_temp else '?'} K")
        return 0

    return asyncio.run(run())


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
        "press": (
            cmd_press,
            "press the status key: GO LIVE when green; --long ends a live without powering off",
        ),
        "pause": (
            cmd_pause,
            "pause stream-pilot (it changes nothing: no setup, restarts or window moves) "
            "or resume it; the status key shows PAUSED",
        ),
        "health": (cmd_health, "wait for the system to be healthy"),
        "seed-obs": (cmd_seed_obs, "seed the OBS configuration"),
        "chromium-prep": (cmd_chromium_prep, "clear Chromium crash flags"),
        "power-guard": (
            cmd_power_guard,
            "make a power-button press power off, even if the controller is hung",
        ),
        "display-mirror": (
            cmd_display_mirror,
            "keep every connected display mirroring the primary one",
        ),
        "selftest-cdp": (
            cmd_selftest_cdp,
            "check that Playwright can attach to the system Chromium over CDP",
        ),
        "record": (
            cmd_record,
            "record the microphone into STREAM_DIR/audio/ until SIGINT (run by "
            "stream-pilot-recorder@.service)",
        ),
        "diag": (
            cmd_diag,
            "collect measurements of a live into STREAM_DIR/diag/ until SIGTERM (run by "
            "stream-pilot-diag@.service)",
        ),
        "upload": (
            cmd_upload,
            "copy the stream folders, status and failure snapshots to [upload] target "
            "(run by stream-pilot-upload.timer)",
        ),
        "rebalance": (
            cmd_rebalance,
            "a frozen camera's white balance: auto for a moment, then frozen again ([wb_freeze])",
        ),
        "upload-key": (
            cmd_upload_key,
            "print the upload SSH public key and a restricted authorized_keys line",
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
    parsers["run"].add_argument(
        "--record",
        metavar="DIR",
        default=None,
        help="save a screenshot and aria snapshot of every Facebook step into DIR "
        "(for selector tuning; may contain the stream key, so DIR is made private)",
    )
    parsers["press"].add_argument(
        "--long",
        action="store_true",
        help="while live, end the live at once and return to setup (on the deck: two presses)",
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
    parsers["record"].add_argument("stream_dir", metavar="STREAM_DIR")
    parsers["diag"].add_argument("stream_dir", metavar="STREAM_DIR")
    parsers["rebalance"].add_argument(
        "--camera", default=None, metavar="NAME",
        help="camera name from the config (default: [wb_freeze] camera)",
    )
    parsers["power-guard"].add_argument(
        "--user",
        default="stream",
        help="user whose stream-pilot status file is checked (default: %(default)s)",
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
