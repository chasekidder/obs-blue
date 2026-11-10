import asyncio
import sys

from stream_pilot import __main__ as cli
from stream_pilot.__main__ import main
from stream_pilot.selftest import selftest_cdp


def test_selftest_cdp_in_help(capsys):
    try:
        main(["--help"])
    except SystemExit as e:
        assert e.code == 0
    assert "selftest-cdp" in capsys.readouterr().out


def test_selftest_missing_binary_fails_fast(tmp_path, capsys):
    missing = tmp_path / "no-such-chromium"
    assert asyncio.run(selftest_cdp(str(missing))) == 1
    err = capsys.readouterr().err
    assert str(missing) in err
    # the binary is checked before playwright is imported
    assert "playwright" not in sys.modules


def test_selftest_cdp_cli_uses_chromium_bin_option(monkeypatch):
    seen = []

    async def fake(chromium_bin):
        seen.append(chromium_bin)
        return 0

    monkeypatch.setattr(cli, "selftest_cdp", fake)
    assert main(["selftest-cdp", "--chromium-bin", "/x/chromium"]) == 0
    assert seen == ["/x/chromium"]


def test_selftest_cdp_cli_defaults_to_config(monkeypatch, tmp_path):
    seen = []

    async def fake(chromium_bin):
        seen.append(chromium_bin)
        return 3

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "selftest_cdp", fake)
    monkeypatch.setattr(cli, "_load_config", lambda: type(
        "C", (), {"cdp": type("D", (), {"chromium_bin": "/cfg/chromium"})()})())
    assert main(["selftest-cdp"]) == 3
    assert seen == ["/cfg/chromium"]


def test_chromium_env_is_isolated(tmp_path, monkeypatch):
    from stream_pilot.selftest import _chromium_env

    monkeypatch.setenv("HOME", "/nonexistent")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
    env = _chromium_env(str(tmp_path / "home"))
    assert env["HOME"] == str(tmp_path / "home")
    assert env["XDG_CONFIG_HOME"].startswith(str(tmp_path))
    assert "XDG_RUNTIME_DIR" not in env
    assert (tmp_path / "home" / ".config").is_dir()
