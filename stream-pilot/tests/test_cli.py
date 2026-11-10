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


def test_subcommands_not_implemented_yet(capsys):
    for argv in (["run", "--dry-run"], ["status"], ["retry"],
                 ["health", "--timeout", "5"]):
        assert main(argv) == 2
        assert "not implemented" in capsys.readouterr().out


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
