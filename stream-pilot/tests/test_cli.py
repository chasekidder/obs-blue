import json
import stat
import tomllib

import pytest

from stream_pilot import __main__ as cli
from stream_pilot.__main__ import main


def test_help_exits_zero(capsys):
    try:
        main(["--help"])
    except SystemExit as e:
        assert e.code == 0
    out = capsys.readouterr().out
    for cmd in ("run", "status", "retry", "health", "seed-obs", "chromium-prep"):
        assert cmd in out


def test_run_help_describes_dry_run(capsys):
    with pytest.raises(SystemExit) as e:
        main(["run", "--help"])
    assert e.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "do everything up to, but not including, clicking Go live" in out
    assert "do not touch real devices" not in out


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def make_defaults(root):
    d = root / "defaults"
    (d / "basic" / "scenes").mkdir(parents=True)
    (d / "basic" / "scenes" / "s.json").write_text(
        '{"a": "@MAIN_CAMERA_DEVICE@", "b": "@JEWELRY_CAMERA_DEVICE@"}')
    return d


def test_seed_obs_cli_end_to_end(home, monkeypatch):
    monkeypatch.setattr(cli, "OBS_DEFAULTS_DIR", make_defaults(home))
    assert main(["seed-obs"]) == 0
    sec = home / ".config" / "stream-pilot" / "secrets.toml"
    pw = tomllib.loads(sec.read_text())["obs"]["password"]
    assert stat.S_IMODE(sec.stat().st_mode) == 0o600
    scene = json.loads((home / ".config/obs-studio/basic/scenes/s.json").read_text())
    assert scene == {"a": "/dev/video0", "b": "/dev/video2"}
    ws = json.loads(
        (home / ".config/obs-studio/plugin_config/obs-websocket/config.json").read_text())
    assert ws["server_password"] == pw
    # the config loader sees the generated password
    from stream_pilot.config import load_config
    assert load_config([sec]).obs.password == pw


def test_seed_obs_cli_missing_defaults_exits_1(home, monkeypatch):
    monkeypatch.setattr(cli, "OBS_DEFAULTS_DIR", home / "nope")
    assert main(["seed-obs"]) == 1
    assert not (home / ".config" / "obs-studio").exists()


def test_seed_obs_cli_other_errors_exit_0(home, monkeypatch, caplog):
    monkeypatch.setattr(cli, "OBS_DEFAULTS_DIR", make_defaults(home))

    def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(cli, "seed_obs", boom)
    assert main(["seed-obs"]) == 0
    assert "denied" in caplog.text


def test_chromium_prep_cli_creates_dir_and_fixes_prefs(home, tmp_path):
    udd = home / ".local" / "share" / "stream-pilot" / "chromium"
    assert main(["chromium-prep"]) == 0
    assert udd.is_dir()
    (udd / "Default").mkdir()
    (udd / "Default" / "Preferences").write_text(
        '{"profile":{"exit_type":"Crashed","exited_cleanly":false}}')
    assert main(["chromium-prep"]) == 0
    prof = json.loads((udd / "Default" / "Preferences").read_text())["profile"]
    assert prof == {"exit_type": "Normal", "exited_cleanly": True}


def test_chromium_prep_cli_errors_exit_0(home, monkeypatch, caplog):
    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(cli, "prep_chromium_profile", boom)
    assert main(["chromium-prep"]) == 0
    assert "disk" in caplog.text


# --- status / health / retry ---------------------------------------------

def write_status_file(home, **overrides):
    import time
    state_dir = home / ".local" / "state" / "stream-pilot"
    state_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    data = {"state": "LIVE", "since": now - 90, "last_error": None,
            "live_id": "777", "memory": {"mem_available_mb": 1234},
            "updated_at": now}
    data.update(overrides)
    (state_dir / "status.json").write_text(json.dumps(data))


def test_status_cli_prints_formatted_status(home, capsys):
    write_status_file(home)
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "state: LIVE" in out
    assert "live id: 777" in out
    assert "mem available: 1234 MB" in out


def test_status_cli_no_file_exits_1(home, capsys):
    assert main(["status"]) == 1
    assert "status.json" in capsys.readouterr().err


def test_status_cli_corrupt_file_exits_1(home, capsys):
    write_status_file(home)
    (home / ".local/state/stream-pilot/status.json").write_text("{oops")
    assert main(["status"]) == 1
    assert capsys.readouterr().err


def test_health_cli_healthy_exits_0(home):
    write_status_file(home, state="READY")
    assert main(["health", "--timeout", "0"]) == 0


def test_health_cli_missing_exits_1(home, capsys):
    assert main(["health", "--timeout", "0"]) == 1
    assert "missing" in capsys.readouterr().err


def test_health_cli_starting_exits_1(home):
    write_status_file(home, state="STARTING")
    assert main(["health", "--timeout", "0"]) == 1


def test_retry_cli_sends_sigusr1_via_injected_runner(home, monkeypatch):
    calls = []

    def fake_runner(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr(cli, "_run_command", fake_runner)
    assert main(["retry"]) == 0
    assert calls == [["systemctl", "--user", "kill", "-s", "SIGUSR1",
                      "stream-pilot.service"]]


def test_retry_cli_propagates_exit_code(home, monkeypatch):
    monkeypatch.setattr(cli, "_run_command", lambda argv: 5)
    assert main(["retry"]) == 5


def test_retry_cli_missing_systemctl_exits_1(home, monkeypatch, capsys):
    def boom(argv):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(cli, "_run_command", boom)
    assert main(["retry"]) == 1
    assert "systemctl" in capsys.readouterr().err


def test_health_cli_config_error_exits_1(home, monkeypatch, capsys):
    def boom():
        raise ValueError("bad toml")

    monkeypatch.setattr(cli, "_load_config", boom)
    assert main(["health", "--timeout", "0"]) == 1
    assert "bad toml" in capsys.readouterr().err


# --- run -----------------------------------------------------------------

@pytest.mark.parametrize("argv, dry", [(["run"], False), (["run", "--dry-run"], True)])
def test_run_cli_loads_config_and_runs_app(home, monkeypatch, argv, dry):
    seen = {}

    async def fake_run_app(cfg, dry_run):
        seen["cfg"] = cfg
        seen["dry_run"] = dry_run
        return 7

    monkeypatch.setattr(cli, "run_app", fake_run_app)
    monkeypatch.setattr(cli, "load_config", lambda paths: ("cfg", list(paths)))
    assert main(argv) == 7
    assert seen["dry_run"] is dry
    cfg_marker, paths = seen["cfg"]
    assert cfg_marker == "cfg"
    # paths are resolved at call time, honouring $HOME
    assert paths == cli._config_paths()
    assert paths[1] == home / ".config/stream-pilot/site.toml"


def test_run_cli_config_error_exits_1(home, monkeypatch, caplog):
    def boom(paths):
        raise ValueError("bad toml")

    monkeypatch.setattr(cli, "load_config", boom)
    assert main(["run"]) == 1
    assert "bad toml" in caplog.text
