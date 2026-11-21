import json
import stat
import tomllib
from datetime import datetime

import pytest

from stream_pilot.config import AudioCfg, CameraCfg
from stream_pilot.seed_obs import clear_stale_sentinels, reset_scenes, seed_obs

CAMS = {
    "main": CameraCfg(scene="Main Camera", device="/dev/v4l/by-id/usb-Main_Cam-video-index0"),
    "jewelry": CameraCfg(scene="Jewlery Camera", device="/dev/video2"),
}
AUDIO = AudioCfg(mic_device="alsa_input.usb-Mic_One-00.mono-fallback")


@pytest.fixture
def defaults(tmp_path):
    d = tmp_path / "defaults"
    (d / "basic" / "scenes").mkdir(parents=True)
    (d / "basic" / "scenes" / "Untitled.json").write_text(
        json.dumps({"main": "@MAIN_CAMERA_DEVICE@", "jew": "@JEWELRY_CAMERA_DEVICE@",
                    "again": "@MAIN_CAMERA_DEVICE@",
                    "mic": "@MICROPHONE_DEVICE@"})
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
                     "again": CAMS["main"].device, "mic": "@MICROPHONE_DEVICE@"}
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


def test_first_seed_substitutes_microphone(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    assert seed_obs(defaults, cfg, sec, CAMS, audio=AUDIO) is True
    scene = json.loads((cfg / "basic" / "scenes" / "Untitled.json").read_text())
    assert scene["mic"] == AUDIO.mic_device
    assert scene["main"] == CAMS["main"].device


def test_microphone_value_is_json_escaped(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    weird = AudioCfg(mic_device='dev"with\\quote')
    seed_obs(defaults, cfg, sec, CAMS, audio=weird)
    scene = json.loads((cfg / "basic" / "scenes" / "Untitled.json").read_text())
    assert scene["mic"] == 'dev"with\\quote'


# --- reset_scenes ----------------------------------------------------------

NEW_CAMS = {
    "main": CameraCfg(scene="Main Camera", device="/dev/video10"),
    "jewelry": CameraCfg(scene="Jewlery Camera", device="/dev/video12"),
}
NEW_AUDIO = AudioCfg(mic_device="alsa_input.new-mic")
NOW = datetime(2026, 11, 9, 20, 15, 30)


def seeded(tmp_path, defaults):
    cfg, sec = paths(tmp_path)
    seed_obs(defaults, cfg, sec, CAMS, audio=AUDIO)
    return cfg


def test_reset_scenes_backs_up_and_substitutes_new_values(tmp_path, defaults):
    cfg = seeded(tmp_path, defaults)
    scenes = cfg / "basic" / "scenes"
    old = (scenes / "Untitled.json").read_text()
    (scenes / "Other.json").write_text('{"user": "scene"}')
    (scenes / "Untitled.json.bak").write_text("obs own backup")
    result = reset_scenes(defaults, cfg, NEW_CAMS, NEW_AUDIO, now=NOW)
    assert result is not None and result.name == "20261109-201530"
    assert (scenes / "Untitled.json.bak-20261109-201530").read_text() == old
    assert (scenes / "Other.json.bak-20261109-201530").read_text() == '{"user": "scene"}'
    # OBS' own .bak and earlier backups are not re-backed-up
    assert not (scenes / "Untitled.json.bak.bak-20261109-201530").exists()
    new = json.loads((scenes / "Untitled.json").read_text())
    assert new == {"main": "/dev/video10", "jew": "/dev/video12",
                   "again": "/dev/video10", "mic": "alsa_input.new-mic"}
    # user scene not in defaults is backed up but left in place
    assert (scenes / "Other.json").read_text() == '{"user": "scene"}'
    # defaults untouched
    assert "@MICROPHONE_DEVICE@" in (defaults / "basic/scenes/Untitled.json").read_text()


def test_reset_scenes_leaves_rest_of_config_alone(tmp_path, defaults):
    cfg = seeded(tmp_path, defaults)
    (cfg / "basic" / "profiles" / "p.ini").write_text("[General]\nedited=1\n")
    reset_scenes(defaults, cfg, NEW_CAMS, NEW_AUDIO, now=NOW)
    assert (cfg / "basic" / "profiles" / "p.ini").read_text() == "[General]\nedited=1\n"


def test_reset_scenes_nothing_to_back_up_returns_none(tmp_path, defaults):
    cfg = tmp_path / "home" / "obs-studio"
    cfg.mkdir(parents=True)
    assert reset_scenes(defaults, cfg, NEW_CAMS, NEW_AUDIO, now=NOW) is None
    scene = json.loads((cfg / "basic" / "scenes" / "Untitled.json").read_text())
    assert scene["mic"] == "alsa_input.new-mic"


def test_reset_scenes_same_second_does_not_overwrite_backup(tmp_path, defaults):
    cfg = seeded(tmp_path, defaults)
    scenes = cfg / "basic" / "scenes"
    first = (scenes / "Untitled.json").read_text()
    reset_scenes(defaults, cfg, NEW_CAMS, NEW_AUDIO, now=NOW)
    reset_scenes(defaults, cfg, CAMS, AUDIO, now=NOW)
    assert (scenes / "Untitled.json.bak-20261109-201530").read_text() == first
    assert len(list(scenes.glob("Untitled.json.bak-*"))) == 2


def test_reset_scenes_missing_defaults_raises_and_keeps_scenes(tmp_path, defaults):
    cfg = seeded(tmp_path, defaults)
    before = (cfg / "basic" / "scenes" / "Untitled.json").read_text()
    with pytest.raises(FileNotFoundError):
        reset_scenes(tmp_path / "nope", cfg, NEW_CAMS, NEW_AUDIO, now=NOW)
    assert (cfg / "basic" / "scenes" / "Untitled.json").read_text() == before
    assert not list((cfg / "basic" / "scenes").glob("*.bak-*"))


def test_clear_stale_sentinels_removes_run_files(tmp_path):
    # As left by OBS sessions that never finished an orderly exit (power cut,
    # forced power-off, or killed while showing the "Crash Detected" dialog).
    sentinel = tmp_path / "obs-studio" / ".sentinel"
    sentinel.mkdir(parents=True)
    for name in ("run_1caf3250-f9c5-49ff-bccb-6947387303ea", "run_768f4135-572b-412d-844a-48343bd4ae37"):
        (sentinel / name).touch()
    (sentinel / "keep.txt").write_text("not ours")
    assert clear_stale_sentinels(tmp_path / "obs-studio") == [
        "run_1caf3250-f9c5-49ff-bccb-6947387303ea", "run_768f4135-572b-412d-844a-48343bd4ae37"]
    assert [p.name for p in sentinel.iterdir()] == ["keep.txt"]


def test_clear_stale_sentinels_nothing_to_do(tmp_path):
    assert clear_stale_sentinels(tmp_path / "obs-studio") == []
    (tmp_path / "obs-studio" / ".sentinel").mkdir(parents=True)
    assert clear_stale_sentinels(tmp_path / "obs-studio") == []
