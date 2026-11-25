"""[upload]: rsync uploads (real rsync into a local directory), SSH options, the feature."""

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from stream_pilot import stream_meta
from stream_pilot import upload as up
from stream_pilot.config import load_config
from stream_pilot.events import PowerPress
from stream_pilot.recording import Recording
from stream_pilot.upload import (
    SERVICE,
    TIMER,
    UPLOADED,
    Upload,
    Uploader,
    authorized_keys_line,
    bwlimit_kib,
    finished_files,
    known_hosts_line,
    parse_target,
    run_lock,
    stream_folders,
)

from test_controller import Rig, make  # noqa: F401 (the rig fixture)
from test_recording import FakeExec

needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")


def cfg_for(tmp_path, target, extra=""):
    site = tmp_path / "site.toml"
    site.write_text(
        f'[paths]\nstate_dir = "{tmp_path}/state"\nstreams_dir = "{tmp_path}/streams"\n'
        f'failures_dir = "{tmp_path}/failures"\n'
        f'[upload]\nenabled = true\ntarget = "{target}"\n{extra}'
    )
    return load_config([site])


def stream(tmp_path, name="2026-11-24_1500-42", *, clips=2, unlisted=True, done=False,
           comments=b'{"text":"21 cookie sheets 3"}\n'):
    d = tmp_path / "streams" / name
    audio = d / "audio"
    audio.mkdir(parents=True)
    lines = [{"type": "session", "session": 1, "started_at": 1.0}]
    for i in range(clips):
        (audio / f"s01-{i:04d}.opus").write_bytes(b"OggS" + bytes([i]) * 100)
        lines.append({"type": "clip", "session": 1, "file": f"s01-{i:04d}.opus",
                      "start_sample": i * 1440000, "end_sample": (i + 1) * 1440000})
    if unlisted:
        (audio / f"s01-{clips:04d}.opus").write_bytes(b"partial")
    (audio / "manifest.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    (audio / ".s01.csv").write_text("x")
    stream_meta.update(d, live_id="42")
    if comments is not None:
        (d / "comments.jsonl").write_bytes(comments)
    if done:
        stream_meta.write_done(d, time.time())
    return d


class Recorded:
    """Runner that records rsync argvs and returns scripted exit codes."""

    def __init__(self, codes=0):
        self.argvs = []
        self.codes = codes

    async def __call__(self, argv):
        self.argvs.append(argv)
        return self.codes if isinstance(self.codes, int) else self.codes.pop(0)


# --------------------------------------------------------------------- pure


def test_parse_target():
    assert parse_target("pc@home.example:inbox") == ("pc@home.example", "home.example", "inbox")
    assert parse_target("home:/srv/in") == ("home", "home", "/srv/in")
    assert parse_target("home") is None
    assert parse_target("pc@:x") is None
    assert parse_target("pc@home:") is None


def test_bwlimit_is_converted_from_kilobits():
    assert bwlimit_kib(200) == 24
    assert bwlimit_kib(1) == 1


def test_known_hosts_line():
    assert known_hosts_line("home", 22, " ssh-ed25519 AAAA ") == "home ssh-ed25519 AAAA\n"
    assert known_hosts_line("home", 2222, "ssh-ed25519 AAAA") == "[home]:2222 ssh-ed25519 AAAA\n"


def test_authorized_keys_line_is_restricted_and_write_only():
    line = authorized_keys_line("ssh-ed25519 AAAA stream-pilot@pc\n", "/srv/in")
    assert line == 'restrict,command="rrsync -wo /srv/in" ssh-ed25519 AAAA stream-pilot@pc'


def test_finished_files_lists_only_manifest_clips(tmp_path):
    d = stream(tmp_path)
    (d / "report.txt").write_text("r")
    assert finished_files(d) == [
        "stream.json", "report.txt", "audio/manifest.jsonl",
        "audio/s01-0000.opus", "audio/s01-0001.opus",
    ]


def test_stream_folders_current_first_then_newest(tmp_path):
    stream(tmp_path, "2026-11-20_1500-1", done=True)
    stream(tmp_path, "2026-11-22_1500-2", done=True)
    stream(tmp_path, "2026-11-24_1500-3")
    gone = stream(tmp_path, "2026-11-19_1500-0", done=True)
    (gone / UPLOADED).write_text("1")
    names = [d.name for d in stream_folders(tmp_path / "streams")]
    assert names == ["2026-11-24_1500-3", "2026-11-22_1500-2", "2026-11-20_1500-1"]


def test_run_lock_is_exclusive(tmp_path):
    with run_lock(tmp_path) as a:
        with run_lock(tmp_path) as b:
            assert a is True and b is False


# --------------------------------------------------------- real rsync, local


@needs_rsync
async def test_live_folder_copies_finished_files_only(tmp_path):
    inbox = tmp_path / "inbox"
    d = stream(tmp_path)
    assert await Uploader(cfg_for(tmp_path, inbox)).upload_stream(d) is False
    out = inbox / d.name
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "audio/manifest.jsonl", "audio/s01-0000.opus", "audio/s01-0001.opus",
        "comments.jsonl", "stream.json",
    ]
    assert not (d / UPLOADED).exists()


@needs_rsync
async def test_comments_are_appended_and_a_partial_line_completed(tmp_path):
    inbox = tmp_path / "inbox"
    d = stream(tmp_path, comments=b'{"text":"a"}\n{"text":"2')
    u = Uploader(cfg_for(tmp_path, inbox))
    await u.upload_stream(d)
    with (d / "comments.jsonl").open("ab") as f:
        f.write(b'6blue 7"}\n{"text":"c"}\n')
    await u.upload_stream(d)
    assert (inbox / d.name / "comments.jsonl").read_bytes() == (d / "comments.jsonl").read_bytes()


@needs_rsync
async def test_done_goes_last_and_marks_the_folder(tmp_path):
    inbox = tmp_path / "inbox"
    d = stream(tmp_path, done=True)
    runner = Recorded()
    u = Uploader(cfg_for(tmp_path, inbox), run=runner)
    assert await u.upload_stream(d) is True
    assert runner.argvs[-1][-2:] == [str(d / "DONE"), f"{inbox}/{d.name}/"]
    assert (d / UPLOADED).exists()
    # and for real:
    (d / UPLOADED).unlink()
    assert await Uploader(cfg_for(tmp_path, inbox)).upload_stream(d) is True
    assert (inbox / d.name / "DONE").exists()


async def test_done_is_not_sent_when_something_failed(tmp_path):
    d = stream(tmp_path, done=True)
    runner = Recorded(codes=[23, 0, 0])
    assert await Uploader(cfg_for(tmp_path, tmp_path / "in"), run=runner).upload_stream(d) is False
    assert len(runner.argvs) == 2  # files, comments; no DONE
    assert not (d / UPLOADED).exists()


@needs_rsync
async def test_heartbeat_only_while_stream_pilot_runs(tmp_path):
    inbox = tmp_path / "inbox"
    cfg = cfg_for(tmp_path, inbox)
    status = cfg.paths.state_dir / "status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"state": "LIVE", "updated_at": time.time() - 1000}))
    await Uploader(cfg).heartbeat()
    assert not (inbox / "_status").exists()
    status.write_text(json.dumps({"state": "LIVE", "updated_at": time.time()}))
    await Uploader(cfg).heartbeat()
    assert json.loads((inbox / "_status" / "status.json").read_text())["state"] == "LIVE"


@needs_rsync
async def test_run_all_copies_failures_and_applies_retention(tmp_path):
    inbox = tmp_path / "inbox"
    cfg = cfg_for(tmp_path, inbox, "retention_days = 90\n")
    fail = cfg.paths.failures_dir / "20261124-150000-setup"
    fail.mkdir(parents=True)
    (fail / "page.html").write_text("<html>")
    old = stream(tmp_path, "2026-08-01_1500-1", done=True)
    os.utime(old / "DONE", (time.time() - 91 * 86400,) * 2)
    recent = stream(tmp_path, "2026-11-20_1500-2", done=True)
    live = stream(tmp_path, "2026-11-24_1500-3")
    assert await Uploader(cfg).run_all() == 0
    assert (inbox / "_failures" / fail.name / "page.html").exists()
    assert not old.exists()  # copied first, then older than 90 days
    assert (inbox / old.name / "DONE").exists()
    assert recent.exists() and (recent / UPLOADED).exists()
    assert live.exists() and not (live / UPLOADED).exists()


def test_retention_never_deletes_an_uncopied_folder(tmp_path):
    cfg = cfg_for(tmp_path, tmp_path / "in", "retention_days = 0\n")
    d = stream(tmp_path, done=True)
    os.utime(d / "DONE", (1.0, 1.0))
    assert Uploader(cfg).retention() == []
    assert d.exists()


# -------------------------------------------------------------- ssh options


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
async def test_remote_upload_never_prompts_and_pins_the_host_key(tmp_path):
    cfg = cfg_for(tmp_path, "pc@home.example:inbox",
                  'host_key = "ssh-ed25519 AAAAC3Nza"\nport = 2222\nconnect_timeout_s = 7\n')
    runner = Recorded()
    keys = tmp_path / "keys"
    u = Uploader(cfg, run=runner, keys_dir=keys)
    assert u.problem() is None
    await u.upload_stream(stream(tmp_path))
    argv = runner.argvs[0]
    assert "--bwlimit=24" in argv and "--partial" in argv and "--timeout=60" in argv
    ssh = argv[argv.index("-e") + 1]
    for opt in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ConnectTimeout=7",
                "ServerAliveInterval=5", "IdentitiesOnly=yes", "GlobalKnownHostsFile=/dev/null",
                f"UserKnownHostsFile={keys}/upload_known_hosts", f"-i {keys}/upload_ed25519",
                "-p 2222"):
        assert opt in ssh
    assert argv[-1] == "pc@home.example:inbox/2026-11-24_1500-42/"
    assert (keys / "upload_known_hosts").read_text() == "[home.example]:2222 ssh-ed25519 AAAAC3Nza\n"
    assert (keys / "upload_ed25519").stat().st_mode & 0o777 == 0o600
    assert (keys / "upload_ed25519.pub").read_text().startswith("ssh-ed25519 ")


def test_problems_are_reported_not_raised(tmp_path):
    assert "no [upload] target" in Uploader(cfg_for(tmp_path, "")).problem()
    assert "host_key" in Uploader(cfg_for(tmp_path, "pc@home:inbox")).problem()
    assert "user@host:path" in Uploader(cfg_for(tmp_path, "pc@home:", 'host_key = "k"\n')).problem()


async def test_run_all_with_a_problem_copies_nothing(tmp_path, caplog):
    runner = Recorded()
    stream(tmp_path)
    assert await Uploader(cfg_for(tmp_path, ""), run=runner).run_all() == 0
    assert runner.argvs == []
    assert "no [upload] target" in caplog.text


async def test_journal_since_prefers_stream_json_then_the_folder_name(tmp_path):
    d = tmp_path / "2026-11-24_1530-42"
    d.mkdir()
    assert up.journal_since(d) == time.mktime(time.strptime("2026-11-24_1530", "%Y-%m-%d_%H%M"))
    stream_meta.update(d, live_confirmed_at=50.0, preroll_started_at=10.0)
    assert up.journal_since(d) == 10.0


# ------------------------------------------------------ through the controller


class JournalExec(FakeExec):
    OUT = {**FakeExec.OUT, "journalctl": "2026-11-24T15:00:00 stream-pilot: state -> LIVE\n"}


def features(on_recording, upload_runner):
    def build(rig):
        uploader = Uploader(rig.cfg, run=upload_runner)
        return [
            Recording(rig.cfg, obs=rig.obs, system=rig.system, exec_fn=JournalExec()),
            Upload(rig.cfg, system=rig.system, uploader=uploader, exec_fn=JournalExec()),
        ]
    return build


def site(tmp_path, *, upload=True, recording=False, final=30):
    return (f"\n[upload]\nenabled = {str(upload).lower()}\ntarget = \"{tmp_path}/inbox\"\n"
            f"final_timeout_s = {final}\n"
            f"\n[recording]\nenabled = {str(recording).lower()}\n")


async def test_off_never_starts_the_timer_and_stops_a_running_one(make, tmp_path):
    runner = Recorded()
    rig = make(features=features(False, runner), site_extra=site(tmp_path, upload=False))
    rig.system.active_units |= {TIMER, SERVICE}
    await rig.to_live()
    sd = rig.c.stream_dir
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert ("system.start_user_unit", TIMER) not in rig.system.calls
    assert rig.system.active_units == set()
    assert runner.argvs == []
    assert not (sd / "journal.txt").exists() and not (sd / "DONE").exists()
    await rig.c.aclose()


async def test_on_starts_the_timer_at_boot(make, tmp_path):
    rig = make(features=features(False, Recorded()), site_extra=site(tmp_path))
    await rig.to_ready()
    await rig.settle()
    assert TIMER in rig.system.active_units
    await rig.c.aclose()


@needs_rsync
async def test_end_writes_the_journal_then_done_then_copies_the_folder(make, tmp_path):
    rig = make(features=features(True, up.run_rsync), site_extra=site(tmp_path, recording=True))
    await rig.to_live()
    sd = rig.c.stream_dir
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert (sd / "journal.txt").read_text().startswith("2026-11-24T15:00:00")
    assert (sd / "journal.txt").stat().st_mtime_ns <= (sd / "DONE").stat().st_mtime_ns
    out = tmp_path / "inbox" / sd.name
    assert (out / "DONE").exists() and (out / "journal.txt").exists()
    assert (sd / UPLOADED).exists()
    assert ("system.stop_user_unit", SERVICE) in rig.system.calls
    assert ("system.poweroff",) in rig.system.calls
    await rig.c.aclose()


async def test_with_recording_off_upload_writes_done_itself(make, tmp_path):
    runner = Recorded()
    rig = make(features=features(False, runner), site_extra=site(tmp_path))
    await rig.to_live()
    sd = rig.c.stream_dir
    await rig.c.handle(PowerPress())
    await rig.settle()
    assert (sd / "DONE").exists()
    assert runner.argvs[-1][-2] == str(sd / "DONE")
    await rig.c.aclose()


async def test_a_hung_final_upload_is_cut_at_final_timeout_and_power_off_continues(make, tmp_path):
    async def hung(argv):
        await asyncio.Event().wait()

    rig = make(features=features(False, hung), site_extra=site(tmp_path, final=0.2))
    await rig.to_live()
    started = time.monotonic()
    await rig.c.handle(PowerPress())
    await asyncio.wait_for(rig.c.wait_idle(), 5)
    assert time.monotonic() - started < 2
    assert ("system.poweroff",) in rig.system.calls
    await rig.c.aclose()


@needs_rsync
async def test_run_rsync_is_killed_when_cancelled(tmp_path):
    src = tmp_path / "big"
    src.write_bytes(os.urandom(4 << 20))
    task = asyncio.ensure_future(up.run_rsync(
        ["rsync", "--bwlimit=1", str(src), str(tmp_path / "copy")]))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert not any("--bwlimit=1" in " ".join(open(f"/proc/{p}/cmdline").read().split("\0"))
                   for p in os.listdir("/proc") if p.isdigit()
                   and Path(f"/proc/{p}/cmdline").exists()
                   and str(src) in open(f"/proc/{p}/cmdline").read())


async def test_worst_case_end_flow_with_recording_and_upload_hung(make, tmp_path, monkeypatch):
    """Everything hangs, including the journal and the final copy: scaled 1:100."""
    from stream_pilot import controller as controller_mod
    from stream_pilot import features as features_mod
    from stream_pilot import power_guard

    scale = 0.01
    for name in ("END_STEP_TIMEOUT_S", "END_FLOW_BUDGET_S", "END_TAIL_RESERVE_S",
                 "END_TAIL_MIN_S", "END_HOOK_TIMEOUT_S"):
        monkeypatch.setattr(controller_mod, name, getattr(controller_mod, name) * scale)
    monkeypatch.setattr(features_mod, "HOOK_TIMEOUT_S", features_mod.HOOK_TIMEOUT_S * scale)

    async def hung_exec(*argv, **kw):
        await asyncio.Event().wait()

    async def hung_rsync(argv):
        await asyncio.Event().wait()

    def build(rig):
        return [
            Recording(rig.cfg, obs=rig.obs, system=rig.system, exec_fn=hung_exec),
            Upload(rig.cfg, system=rig.system, uploader=Uploader(rig.cfg, run=hung_rsync),
                   exec_fn=hung_exec),
        ]

    rig = make(features=build, site_extra=site(tmp_path, recording=True, final=30 * scale))
    await rig.to_live()
    hang = asyncio.Event()
    for obj, names in ((rig.obs, ["stop_stream", "stream_active"]), (rig.capture, ["stop"]),
                       (rig.fb, ["close_browser"]), (rig.system, ["stop_user_unit"])):
        for n in names:
            obj.gates[n] = hang
    started = time.monotonic()
    await rig.c.handle(PowerPress())
    await asyncio.wait_for(rig.c.wait_idle(), 5)
    assert ("system.poweroff",) in rig.system.calls
    assert time.monotonic() - started < (power_guard.GRACE_S - 15) * scale
    await rig.c.aclose()


async def test_a_run_stops_at_its_budget_and_the_live_folder_goes_first(tmp_path):
    cfg = cfg_for(tmp_path, tmp_path / "inbox")
    fail = cfg.paths.failures_dir / "20261124-150000-setup"
    fail.mkdir(parents=True)
    (fail / "page.html").write_text("x")
    old = stream(tmp_path, "2026-11-20_1500-1", done=True)
    live = stream(tmp_path, "2026-11-24_1500-3")
    order = []

    async def slow(argv):
        order.append(argv[-1])
        if "_failures" in argv[-1]:
            await asyncio.Event().wait()  # a huge backlog
        return 0

    started = time.monotonic()
    assert await Uploader(cfg, run=slow).run_all(budget_s=0.3) == 0
    assert time.monotonic() - started < 1.5
    assert order[0].endswith(f"{live.name}/") and any(o.endswith(f"{old.name}/") for o in order)
    assert order[-1].endswith("_failures/")


@needs_rsync
async def test_a_cut_off_copy_keeps_its_partial_file(tmp_path):
    src = tmp_path / "big"
    src.write_bytes(os.urandom(2 << 20))
    dest = tmp_path / "out"
    dest.mkdir()
    task = asyncio.ensure_future(up.run_rsync(
        ["rsync", "--partial", "--bwlimit=200", str(src), f"{dest}/"]))
    await asyncio.sleep(1.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    kept = dest / "big"
    assert kept.exists() and 0 < kept.stat().st_size < src.stat().st_size
