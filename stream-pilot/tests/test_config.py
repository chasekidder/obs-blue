import logging
from pathlib import Path

import pytest

from stream_pilot.config import DEFAULT_PATHS, Config, load_config


def test_defaults_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config([])
    assert isinstance(cfg, Config)
    assert cfg.cameras["jewelry"].scene == "Jewlery Camera"
    assert cfg.cameras["main"].scene == "Main Camera"
    assert cfg.cameras["main"].device == "/dev/video0"
    assert cfg.cameras["jewelry"].device == "/dev/video2"
    assert cfg.obs.port == 4455
    assert cfg.cdp.url == "http://127.0.0.1:9222"
    assert cfg.cdp.chromium_bin == "/usr/bin/chromium-browser"
    assert cfg.ssn.enabled is True
    assert cfg.memory.floor_mb == 600
    assert cfg.memory.auto_reload_popout is False
    assert cfg.deck.long_press_s == 3.0
    assert cfg.deck.brightness == 70
    assert cfg.timeouts.attempts == 3
    assert cfg.timeouts.step_s == 30


def test_site_file_merges_keeping_other_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    site = tmp_path / "site.toml"
    site.write_text(
        '[facebook]\nactor_id = "111"\n\n[cameras.main]\ndevice = "/dev/video4"\n'
    )
    cfg = load_config([site])
    assert cfg.facebook.actor_id == "111"
    assert cfg.facebook.group_id == ""
    assert cfg.facebook.producer_url_template.startswith("https://www.facebook.com/")
    assert cfg.cameras["main"].device == "/dev/video4"
    assert cfg.cameras["main"].scene == "Main Camera"
    assert cfg.cameras["jewelry"].device == "/dev/video2"


def test_later_files_win(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_text('[obs]\npassword = "one"\nport = 1\n')
    b.write_text('[obs]\npassword = "two"\n')
    cfg = load_config([a, b])
    assert cfg.obs.password == "two"
    assert cfg.obs.port == 1


def test_extra_camera_keys_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    site = tmp_path / "site.toml"
    site.write_text('[cameras.wide]\nscene = "Wide"\ndevice = "/dev/video6"\n')
    cfg = load_config([site])
    assert set(cfg.cameras) == {"main", "jewelry", "wide"}
    assert cfg.cameras["wide"].scene == "Wide"


def test_producer_url_formats_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    site = tmp_path / "site.toml"
    site.write_text('[facebook]\nactor_id = "111"\ngroup_id = "222"\n')
    cfg = load_config([site])
    assert cfg.facebook.producer_url == (
        "https://www.facebook.com/live/producer/?actor=111&entry_point=group&target_id=222"
    )


def test_missing_site_values_all_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config([])
    assert cfg.missing_site_values() == [
        "facebook.actor_id",
        "facebook.group_id",
        "facebook.group_name",
    ]


def test_missing_site_values_whitespace_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    site = tmp_path / "site.toml"
    site.write_text(
        '[facebook]\nactor_id = "  "\ngroup_id = "222"\ngroup_name = "\\t"\n'
    )
    cfg = load_config([site])
    assert cfg.missing_site_values() == ["facebook.actor_id", "facebook.group_name"]


def test_missing_site_values_filled(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    site = tmp_path / "site.toml"
    site.write_text(
        '[facebook]\nactor_id = "1"\ngroup_id = "2"\ngroup_name = "Gems"\n'
    )
    assert load_config([site]).missing_site_values() == []


def test_tilde_expands_in_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config([])
    assert cfg.cdp.user_data_dir == tmp_path / ".local/share/stream-pilot/chromium"
    assert cfg.paths.state_dir == tmp_path / ".local/state/stream-pilot"
    assert cfg.paths.streams_dir == tmp_path / "streams"
    assert cfg.paths.failures_dir == tmp_path / ".local/state/stream-pilot/failures"
    assert isinstance(cfg.paths.state_dir, Path)


def test_tilde_expands_in_site_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    site = tmp_path / "site.toml"
    site.write_text('[paths]\nstreams_dir = "~/vids"\n')
    assert load_config([site]).paths.streams_dir == tmp_path / "vids"


def test_missing_files_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config([tmp_path / "nope.toml", tmp_path / "also-nope.toml"])
    assert cfg.obs.host == "127.0.0.1"


def test_unknown_keys_ignored_with_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HOME", str(tmp_path))
    site = tmp_path / "site.toml"
    site.write_text(
        '[facebook]\nactor_idd = "typo"\nactor_id = "5"\n\n[bogus]\nx = 1\n'
        '\n[cameras.main]\nzoom = 3\n'
    )
    with caplog.at_level(logging.WARNING, logger="stream_pilot.config"):
        cfg = load_config([site])
    assert cfg.facebook.actor_id == "5"
    text = caplog.text
    assert "actor_idd" in text
    assert "bogus" in text
    assert "zoom" in text


def test_default_paths_shape():
    assert DEFAULT_PATHS[0] == Path("/etc/stream-pilot/config.toml")
    assert DEFAULT_PATHS[1].name == "site.toml"
    assert DEFAULT_PATHS[2].name == "secrets.toml"
    assert all(p.is_absolute() for p in DEFAULT_PATHS)


def test_config_is_frozen(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config([])
    with pytest.raises(Exception):
        cfg.obs.port = 1  # type: ignore[misc]


def test_invalid_toml_skipped_with_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HOME", str(tmp_path))
    bad = tmp_path / "site.toml"
    bad.write_text("[facebook\nactor_id = ")
    with caplog.at_level(logging.WARNING, logger="stream_pilot.config"):
        cfg = load_config([bad])
    assert cfg.obs.port == 4455
    assert "site.toml" in caplog.text


def test_non_table_section_falls_back_to_defaults(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HOME", str(tmp_path))
    site = tmp_path / "site.toml"
    site.write_text('obs = 5\n')
    with caplog.at_level(logging.WARNING, logger="stream_pilot.config"):
        cfg = load_config([site])
    assert cfg.obs.port == 4455
    assert "obs" in caplog.text
