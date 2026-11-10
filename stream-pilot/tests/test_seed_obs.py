import json
import stat
import tomllib

import pytest

from stream_pilot.config import CameraCfg
from stream_pilot.seed_obs import seed_obs

CAMS = {
    "main": CameraCfg(scene="Main Camera", device="/dev/v4l/by-id/usb-Main_Cam-video-index0"),
    "jewelry": CameraCfg(scene="Jewlery Camera", device="/dev/video2"),
}


@pytest.fixture
def defaults(tmp_path):
    d = tmp_path / "defaults"
    (d / "basic" / "scenes").mkdir(parents=True)
    (d / "basic" / "scenes" / "Untitled.json").write_text(
        json.dumps({"main": "@MAIN_CAMERA_DEVICE@", "jew": "@JEWELRY_CAMERA_DEVICE@",
                    "again": "@MAIN_CAMERA_DEVICE@"})
    )
    (d / "basic" / "profiles").mkdir()
    (d / "basic" / "profiles" / "p.ini").write_text("[General]\n")
    return d


def paths(tmp_path):
    return tmp_path / "home" / "obs-studio", tmp_path / "home" / "sp" / "secrets.toml"


def ws_json(config_dir):
    return json.loads(
        (config_dir / "plugin_config" / "obs-websocket" / "config.json").read_text()
    )


def secrets(path):
    return tomllib.loads(path.read_text())


def test_first_seed_copies_and_substitutes(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    assert seed_obs(defaults, cfg, sec, CAMS) is True
    scene = json.loads((cfg / "basic" / "scenes" / "Untitled.json").read_text())
    assert scene == {"main": CAMS["main"].device, "jew": "/dev/video2",
                     "again": CAMS["main"].device}
    assert (cfg / "basic" / "profiles" / "p.ini").exists()
    # defaults untouched
    assert "@MAIN_CAMERA_DEVICE@" in (defaults / "basic/scenes/Untitled.json").read_text()


def test_websocket_config_exact(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    seed_obs(defaults, cfg, sec, CAMS)
    pw = secrets(sec)["obs"]["password"]
    assert ws_json(cfg) == {
        "alerts_enabled": False, "auth_required": True, "first_load": False,
        "server_enabled": True, "server_password": pw, "server_port": 4455,
    }
    assert len(pw) >= 24


def test_second_seed_keeps_user_edits_but_refreshes_websocket(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    seed_obs(defaults, cfg, sec, CAMS)
    scene = cfg / "basic" / "scenes" / "Untitled.json"
    scene.write_text('{"edited": true}')
    ws = cfg / "plugin_config" / "obs-websocket" / "config.json"
    ws.write_text('{"server_password": "stale"}')
    assert seed_obs(defaults, cfg, sec, CAMS) is False
    assert scene.read_text() == '{"edited": true}'
    assert ws_json(cfg)["server_password"] == secrets(sec)["obs"]["password"]
    assert ws_json(cfg)["auth_required"] is True


def test_password_persists_across_runs(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    seed_obs(defaults, cfg, sec, CAMS)
    pw1 = secrets(sec)["obs"]["password"]
    seed_obs(defaults, cfg, sec, CAMS)
    assert secrets(sec)["obs"]["password"] == pw1


def test_other_secret_keys_preserved(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    sec.parent.mkdir(parents=True)
    sec.write_text('[ssn]\nsession_id = "a\\"b\\\\c"\n[obs]\nextra = 5\n[top]\nflag = true\nf = 1.5\n')
    seed_obs(defaults, cfg, sec, CAMS)
    data = secrets(sec)
    assert data["ssn"] == {"session_id": 'a"b\\c'}
    assert data["obs"]["extra"] == 5
    assert data["obs"]["password"]
    assert data["top"] == {"flag": True, "f": 1.5}


def test_existing_password_kept_and_file_not_rewritten(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    sec.parent.mkdir(parents=True)
    sec.write_text('# my comment\n[obs]\npassword = "hunter2"\n')
    sec.chmod(0o600)
    before = sec.read_text()
    seed_obs(defaults, cfg, sec, CAMS)
    assert sec.read_text() == before
    assert ws_json(cfg)["server_password"] == "hunter2"


def test_secrets_mode_0600_and_parent_0700(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    seed_obs(defaults, cfg, sec, CAMS)
    assert stat.S_IMODE(sec.stat().st_mode) == 0o600
    assert stat.S_IMODE(sec.parent.stat().st_mode) == 0o700


def test_loose_existing_mode_is_fixed(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    sec.parent.mkdir(parents=True)
    sec.write_text('[obs]\npassword = "pw"\n')
    sec.chmod(0o644)
    seed_obs(defaults, cfg, sec, CAMS)
    assert stat.S_IMODE(sec.stat().st_mode) == 0o600


def test_corrupt_secrets_starts_fresh(tmp_path, defaults, caplog):
    cfg, sec = paths(tmp_path)
    sec.parent.mkdir(parents=True)
    sec.write_text("this is [not toml")
    seed_obs(defaults, cfg, sec, CAMS)
    assert secrets(sec)["obs"]["password"]
    assert "secrets" in caplog.text


def test_empty_password_regenerated(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    sec.parent.mkdir(parents=True)
    sec.write_text('[obs]\npassword = ""\n')
    seed_obs(defaults, cfg, sec, CAMS)
    assert secrets(sec)["obs"]["password"]


def test_missing_defaults_raises(tmp_path):
    cfg, sec = paths(tmp_path)
    with pytest.raises(FileNotFoundError):
        seed_obs(tmp_path / "nope", cfg, sec, CAMS)
    assert not cfg.exists()


def test_no_temp_files_left(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    seed_obs(defaults, cfg, sec, CAMS)
    assert [p.name for p in sec.parent.iterdir()] == ["secrets.toml"]
    ws_dir = cfg / "plugin_config" / "obs-websocket"
    assert [p.name for p in ws_dir.iterdir()] == ["config.json"]
