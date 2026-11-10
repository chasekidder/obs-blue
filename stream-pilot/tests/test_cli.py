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
                 ["health", "--timeout", "5"], ["seed-obs"], ["chromium-prep"]):
        assert main(argv) == 2
        assert "not implemented" in capsys.readouterr().out
