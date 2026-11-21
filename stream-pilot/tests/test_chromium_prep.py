import json
import os

from stream_pilot.chromium_prep import clear_singleton_lock, prep_chromium_profile


def write_prefs(root, data):
    d = root / "Default"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Preferences").write_text(json.dumps(data))
    return d / "Preferences"


def test_fixes_crash_flags_and_preserves_rest(tmp_path):
    p = write_prefs(tmp_path, {
        "profile": {"exit_type": "Crashed", "exited_cleanly": False, "name": "x"},
        "other": [1, 2],
    })
    assert prep_chromium_profile(tmp_path) is True
    data = json.loads(p.read_text())
    assert data["profile"] == {"exit_type": "Normal", "exited_cleanly": True, "name": "x"}
    assert data["other"] == [1, 2]
    assert " " not in p.read_text()  # compact like Chromium
    assert [x.name for x in p.parent.iterdir()] == ["Preferences"]


def test_creates_profile_section_when_missing(tmp_path):
    p = write_prefs(tmp_path, {"a": 1})
    assert prep_chromium_profile(tmp_path) is True
    assert json.loads(p.read_text())["profile"] == {
        "exit_type": "Normal", "exited_cleanly": True}


def test_already_clean_returns_false_and_no_rewrite(tmp_path):
    p = write_prefs(tmp_path, {"profile": {"exit_type": "Normal", "exited_cleanly": True}})
    before = p.stat().st_mtime_ns
    assert prep_chromium_profile(tmp_path) is False
    assert p.stat().st_mtime_ns == before


def test_missing_preferences_returns_false(tmp_path):
    assert prep_chromium_profile(tmp_path) is False
    assert prep_chromium_profile(tmp_path / "absent") is False


def test_corrupt_preferences_tolerated(tmp_path, caplog):
    p = write_prefs(tmp_path, {})
    p.write_text("{not json")
    assert prep_chromium_profile(tmp_path) is False
    assert p.read_text() == "{not json"
    assert "Preferences" in caplog.text


def test_non_object_json_tolerated(tmp_path):
    p = write_prefs(tmp_path, {})
    p.write_text("[1,2]")
    assert prep_chromium_profile(tmp_path) is False


def test_clear_singleton_lock_removes_dangling_lock_links(tmp_path):
    # As left by a previous boot whose hostname differed ("fedora" vs "stream-pc.lan").
    os.symlink("stream-pc.lan-3140", tmp_path / "SingletonLock")
    os.symlink("14239420397549186689", tmp_path / "SingletonCookie")
    os.symlink("/tmp/org.chromium.Chromium.gone/SingletonSocket", tmp_path / "SingletonSocket")
    (tmp_path / "Local State").write_text("{}")
    assert clear_singleton_lock(tmp_path) == ["SingletonCookie", "SingletonLock", "SingletonSocket"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Local State"]


def test_clear_singleton_lock_nothing_to_do(tmp_path):
    assert clear_singleton_lock(tmp_path) == []
    assert clear_singleton_lock(tmp_path / "absent") == []


def test_profile_not_a_dict_is_replaced(tmp_path):
    p = write_prefs(tmp_path, {"profile": "weird"})
    assert prep_chromium_profile(tmp_path) is True
    assert json.loads(p.read_text())["profile"]["exit_type"] == "Normal"
