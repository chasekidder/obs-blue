import json
import os
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


def test_seed_obs_cli_clears_stale_obs_sentinel_even_if_seeding_fails(home, monkeypatch):
    sentinel = home / ".config" / "obs-studio" / ".sentinel"
    sentinel.mkdir(parents=True)
    (sentinel / "run_71360851-8a67-46d1-b9c5-3a94bb8b41a4").touch()

    def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(cli, "seed_obs", boom)
    assert main(["seed-obs"]) == 0
    assert list(sentinel.iterdir()) == []


def make_scenes_defaults(root):
    d = root / "defaults"
    (d / "basic" / "scenes").mkdir(parents=True)
    (d / "basic" / "scenes" / "s.json").write_text(
        '{"a": "@MAIN_CAMERA_DEVICE@", "b": "@JEWELRY_CAMERA_DEVICE@",'
        ' "m": "@MICROPHONE_DEVICE@"}')
    return d


def test_seed_obs_help_describes_reset_scenes(capsys):
    with pytest.raises(SystemExit) as e:
        main(["seed-obs", "--help"])
    assert e.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "--reset-scenes" in out
    assert ("re-apply camera/microphone IDs from site.toml (backs up the current "
            "scene collection); stop OBS first") in out


def test_seed_obs_cli_substitutes_microphone_on_first_seed(home, monkeypatch):
    monkeypatch.setattr(cli, "OBS_DEFAULTS_DIR", make_scenes_defaults(home))
    assert main(["seed-obs"]) == 0
    scene = json.loads((home / ".config/obs-studio/basic/scenes/s.json").read_text())
    assert scene["m"] == "alsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback"


def test_seed_obs_cli_reset_scenes_applies_new_site_values(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "OBS_DEFAULTS_DIR", make_scenes_defaults(home))
    assert main(["seed-obs"]) == 0
    sec = home / ".config" / "stream-pilot" / "secrets.toml"
    pw = tomllib.loads(sec.read_text())["obs"]["password"]
    site = home / ".config" / "stream-pilot" / "site.toml"
    site.write_text(
        '[cameras.main]\ndevice = "/dev/video20"\n'
        '[cameras.jewelry]\ndevice = "/dev/video22"\n'
        '[audio]\nmic_device = "alsa_input.site-mic"\n')
    capsys.readouterr()
    assert main(["seed-obs"]) == 0  # plain seed: one-time, nothing changes
    scenes = home / ".config/obs-studio/basic/scenes"
    assert json.loads((scenes / "s.json").read_text())["a"] == "/dev/video0"
    assert main(["seed-obs", "--reset-scenes"]) == 0
    assert json.loads((scenes / "s.json").read_text()) == {
        "a": "/dev/video20", "b": "/dev/video22", "m": "alsa_input.site-mic"}
    backups = list(scenes.glob("s.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["a"] == "/dev/video0"
    assert backups[0].name in capsys.readouterr().out
    # the normal seed work (websocket config, password) still happens
    ws = json.loads(
        (home / ".config/obs-studio/plugin_config/obs-websocket/config.json").read_text())
    assert ws["server_password"] == pw


def test_seed_obs_cli_reset_scenes_missing_defaults_exits_1(home, monkeypatch):
    monkeypatch.setattr(cli, "OBS_DEFAULTS_DIR", make_scenes_defaults(home))
    assert main(["seed-obs"]) == 0
    monkeypatch.setattr(cli, "OBS_DEFAULTS_DIR", home / "nope")
    assert main(["seed-obs", "--reset-scenes"]) == 1


def test_seed_obs_cli_reset_scenes_other_errors_exit_0(home, monkeypatch):
    monkeypatch.setattr(cli, "OBS_DEFAULTS_DIR", make_scenes_defaults(home))

    def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(cli, "reset_scenes", boom)
    assert main(["seed-obs", "--reset-scenes"]) == 0


def test_seed_obs_cli_without_flag_does_not_reset(home, monkeypatch):
    monkeypatch.setattr(cli, "OBS_DEFAULTS_DIR", make_scenes_defaults(home))

    def boom(*a, **k):
        raise AssertionError("reset_scenes must not run without the flag")

    monkeypatch.setattr(cli, "reset_scenes", boom)
    assert main(["seed-obs"]) == 0


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


def test_chromium_prep_cli_clears_stale_profile_lock(home):
    udd = home / ".local" / "share" / "stream-pilot" / "chromium"
    udd.mkdir(parents=True)
    os.symlink("stream-pc.lan-3140", udd / "SingletonLock")
    assert main(["chromium-prep"]) == 0
    assert not (udd / "SingletonLock").is_symlink()


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
    assert calls == [["systemctl", "--user", "kill", "--kill-whom=main", "-s", "SIGUSR1",
                      "stream-pilot.service"]]


def test_press_cli_sends_sigusr2_via_injected_runner(home, monkeypatch):
    calls = []

    def fake_runner(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr(cli, "_run_command", fake_runner)
    assert main(["press"]) == 0
    assert calls == [["systemctl", "--user", "kill", "--kill-whom=main", "-s", "SIGUSR2",
                      "stream-pilot.service"]]


def test_display_mirror_cli_runs_the_mirror_loop(home, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_display_mirror", lambda: calls.append("run") or 0)
    assert main(["display-mirror"]) == 0
    assert calls == ["run"]


def test_press_long_cli_sends_sigrtmin(home, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_run_command", lambda argv: calls.append(argv) or 0)
    assert main(["press", "--long"]) == 0
    assert calls == [["systemctl", "--user", "kill", "--kill-whom=main", "-s", "SIGRTMIN",
                      "stream-pilot.service"]]


def test_press_cli_missing_systemctl_exits_1(home, monkeypatch, capsys):
    def boom(argv):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(cli, "_run_command", boom)
    assert main(["press"]) == 1
    assert "systemctl" in capsys.readouterr().err


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

    async def fake_run_app(cfg, dry_run, record_dir=None):
        seen["cfg"] = cfg
        seen["dry_run"] = dry_run
        seen["record_dir"] = record_dir
        return 7

    monkeypatch.setattr(cli, "run_app", fake_run_app)
    monkeypatch.setattr(cli, "load_config", lambda paths: ("cfg", list(paths)))
    assert main(argv) == 7
    assert seen["dry_run"] is dry
    assert seen["record_dir"] is None
    cfg_marker, paths = seen["cfg"]
    assert cfg_marker == "cfg"
    # paths are resolved at call time, honouring $HOME
    assert paths == cli._config_paths()
    assert paths[1] == home / ".config/stream-pilot/site.toml"


def test_run_cli_record_dir_is_expanded_created_and_passed(home, monkeypatch):
    seen = {}

    async def fake_run_app(cfg, dry_run, record_dir=None):
        seen["record_dir"] = record_dir
        return 0

    monkeypatch.setattr(cli, "run_app", fake_run_app)
    monkeypatch.setattr(cli, "load_config", lambda paths: "cfg")
    assert main(["run", "--record", "~/rec/one"]) == 0
    assert seen["record_dir"] == home / "rec" / "one"
    assert seen["record_dir"].is_dir()
    assert oct(seen["record_dir"].stat().st_mode & 0o777) == "0o700"  # captures may hold the stream key


def test_run_cli_record_dir_that_cannot_be_created_exits_1(home, monkeypatch, caplog):
    async def fake_run_app(cfg, dry_run, record_dir=None):
        raise AssertionError("must not run")

    monkeypatch.setattr(cli, "run_app", fake_run_app)
    monkeypatch.setattr(cli, "load_config", lambda paths: "cfg")
    (home / "afile").write_text("x")
    assert main(["run", "--record", str(home / "afile" / "sub")]) == 1
    assert "record" in caplog.text


def test_run_cli_config_error_exits_1(home, monkeypatch, caplog):
    def boom(paths):
        raise ValueError("bad toml")

    monkeypatch.setattr(cli, "load_config", boom)
    assert main(["run"]) == 1
    assert "bad toml" in caplog.text
