import io
import json
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from stream_pilot import recorder as rec_mod
from stream_pilot.config import AudioCfg, RecordingCfg
from stream_pilot.recorder import (
    Manifest,
    Recorder,
    clips,
    ffmpeg_argv,
    last_session,
    opus_samples,
    parse_segment_line,
    parse_start,
)

AUDIO = AudioCfg(mic_device="alsa_input.test-mic")
REC = RecordingCfg(enabled=True, bitrate_kbps=32, clip_s=30, fb_delay_s=15,
                   replay_includes_preroll=False)


def test_ffmpeg_argv_records_the_mic_as_mono_opus_clips(tmp_path):
    argv = ffmpeg_argv("alsa_input.test-mic", tmp_path, 2, REC)
    joined = " ".join(argv)
    assert argv[0] == "ffmpeg"
    assert "-f pulse" in joined and "-i alsa_input.test-mic" in joined
    assert "-ac 1" in joined and "-c:a libopus -b:a 32k" in joined
    assert "-f segment -segment_time 30 -segment_format opus" in joined
    assert argv[-1] == str(tmp_path / "s02-%04d.opus")
    assert str(tmp_path / ".s02.csv") in argv


def test_parse_start():
    assert parse_start("  Duration: N/A, start: 1795552380.464252, bitrate: 768 kb/s") == (
        1795552380.464252
    )
    assert parse_start("  Duration: N/A, start: 0.000000, bitrate: 1 kb/s") is None
    assert parse_start("Stream mapping:") is None


def test_parse_segment_line():
    assert parse_segment_line("s01-0003.opus,90.000000,120.020000\n") == (
        "s01-0003.opus", 90.0, 120.02
    )
    assert parse_segment_line("garbage") is None
    assert parse_segment_line("a,b,c") is None


def test_manifest_never_glues_onto_a_cut_line(tmp_path):
    path = tmp_path / "manifest.jsonl"
    path.write_bytes(b'{"type":"clip","session":1,"file":"s01-0000.opus"}\n{"type":"cl')
    m = Manifest(path)
    m.append({"type": "session", "session": 2, "started_at": 5.0})
    recs = m.records()
    assert [r["type"] for r in recs] == ["clip", "session"]
    assert clips(recs) == ["s01-0000.opus"]


def test_last_session_end_is_its_last_clip():
    recs = [
        {"type": "session", "session": 1, "started_at": 100.0},
        {"type": "clip", "session": 1, "file": "a", "end_sample": 48000 * 30},
        {"type": "clip", "session": 1, "file": "b", "end_sample": 48000 * 60},
    ]
    assert last_session(recs) == (1, 160.0)
    assert last_session([]) == (0, None)
    assert last_session([{"type": "session", "session": 3, "started_at": 7.0}]) == (3, 7.0)


class FakeFfmpeg:
    """Writes the segment list like ffmpeg: one line per finished clip, the last on SIGINT."""

    def __init__(self, argv, *, start=1000.0, clips_before_stop=2, exit_code=None, **_kw):
        self.argv = argv
        self.csv = Path(argv[argv.index("-segment_list") + 1])
        self.pattern = argv[-1]
        self.returncode = None
        self.signals = []
        self.stderr = io.StringIO(f"Input #0, pulse\n  Duration: N/A, start: {start:.6f}, bitrate: 768 kb/s\n")
        self.exit_code = exit_code
        self._n = 0
        for _ in range(clips_before_stop):
            self._clip()
        if exit_code is not None:
            self.returncode = exit_code

    def _clip(self):
        name = Path(self.pattern % self._n).name
        Path(self.pattern % self._n).write_bytes(b"OggS")
        with self.csv.open("a") as f:
            f.write(f"{name},{self._n * 30:.6f},{(self._n + 1) * 30:.6f}\n")
        self._n += 1

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGINT and self.returncode is None:
            self._clip()
            self.returncode = 255

    def kill(self):
        self.returncode = -9


def run_recorder(tmp_path, procs, *, stop_after=0.05):
    made = []

    def popen(argv, **kw):
        spec = procs[min(len(made), len(procs) - 1)]
        p = FakeFfmpeg(argv, **spec)
        made.append(p)
        return p

    r = Recorder(tmp_path, AUDIO, REC, popen=popen, sleep=lambda s: time.sleep(0.001))
    timer = threading.Timer(stop_after, r.request_stop)
    timer.start()
    try:
        assert r.run() == 0
    finally:
        timer.cancel()
    return r, made


def test_recording_lists_clips_with_sample_offsets_and_the_anchor(tmp_path, monkeypatch):
    monkeypatch.setattr(rec_mod, "ANCHOR_WAIT_S", 0.5)
    r, made = run_recorder(tmp_path, [{"start": 1795552000.25}])
    recs = r.manifest.records()
    session = recs[0]
    assert session == {
        "type": "session", "session": 1, "started_at": 1795552000.25, "anchor": "pulse",
        "mic_device": "alsa_input.test-mic", "sample_rate": 48000, "channels": 1,
        "bitrate_kbps": 32, "clip_s": 30,
    }
    clip_recs = [x for x in recs if x["type"] == "clip"]
    assert [c["file"] for c in clip_recs] == ["s01-0000.opus", "s01-0001.opus", "s01-0002.opus"]
    assert [(c["start_sample"], c["end_sample"]) for c in clip_recs] == [
        (0, 1440000), (1440000, 2880000), (2880000, 4320000)
    ]
    assert clip_recs[1]["start_at"] == 1795552030.25
    assert recs[-1]["type"] == "session_end" and recs[-1]["reason"] == "stopped"
    assert made[0].signals == [signal.SIGINT]  # the last clip was finalized, not killed


def test_a_second_run_is_a_new_session_with_the_gap_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(rec_mod, "ANCHOR_WAIT_S", 0.5)
    run_recorder(tmp_path, [{"start": 1795552000.0, "clips_before_stop": 1}])  # ends at +60 s
    r, _ = run_recorder(tmp_path, [{"start": 1795552500.0, "clips_before_stop": 0}])
    recs = r.manifest.records()
    gap = next(x for x in recs if x["type"] == "gap")
    assert gap == {"type": "gap", "session": 2, "from": 1795552060.0, "to": 1795552500.0}
    assert [c["file"] for c in recs if c["type"] == "clip"][-1] == "s02-0000.opus"


def test_ffmpeg_dying_starts_a_new_session(tmp_path, monkeypatch):
    monkeypatch.setattr(rec_mod, "ANCHOR_WAIT_S", 0.2)
    monkeypatch.setattr(rec_mod, "RESTART_BACKOFF_S", 0.01)
    r, made = run_recorder(
        tmp_path, [{"start": 1000.0, "clips_before_stop": 1, "exit_code": 1},
                   {"start": 1100.0, "clips_before_stop": 0}],
        stop_after=0.3,
    )
    recs = r.manifest.records()
    ends = [x for x in recs if x["type"] == "session_end"]
    assert ends[0]["reason"] == "ffmpeg exited with status 1"
    assert any(x["type"] == "session" and x["session"] == 2 for x in recs)
    assert len(made) >= 2


def test_missing_anchor_falls_back_to_the_process_start(tmp_path, monkeypatch):
    monkeypatch.setattr(rec_mod, "ANCHOR_WAIT_S", 0.05)

    class NoStart(FakeFfmpeg):
        def __init__(self, argv, **kw):
            super().__init__(argv, **kw)
            self.stderr = io.StringIO("")

    r = Recorder(tmp_path, AUDIO, REC, popen=lambda argv, **kw: NoStart(argv),
                 clock=lambda: 4242.0, sleep=lambda s: time.sleep(0.001))
    threading.Timer(0.1, r.request_stop).start()
    r.run()
    session = r.manifest.records()[0]
    assert session["anchor"] == "process_start" and session["started_at"] == 4242.0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_real_ffmpeg_finalizes_the_last_clip_on_sigint(tmp_path, monkeypatch):
    """The real segment muxer and list, with a test tone instead of PulseAudio."""
    real = rec_mod.ffmpeg_argv

    def tone_argv(mic, audio_dir, session, rec):
        argv = real(mic, audio_dir, session, rec)
        i = argv.index("pulse")
        argv[i - 1: i + 9] = ["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        return argv

    monkeypatch.setattr(rec_mod, "ffmpeg_argv", tone_argv)
    monkeypatch.setattr(rec_mod, "ANCHOR_WAIT_S", 0.5)
    rec = RecordingCfg(enabled=True, bitrate_kbps=32, clip_s=1, fb_delay_s=15,
                       replay_includes_preroll=False)
    r = Recorder(tmp_path, AUDIO, rec)
    threading.Timer(2.6, r.request_stop).start()
    threading.Timer(2.62, r.request_stop).start()  # e.g. SIGINT and SIGTERM: still one SIGINT
    r.run()
    recs = r.manifest.records()
    names = clips(recs)
    assert len(names) >= 3
    assert all((tmp_path / "audio" / n).stat().st_size > 0 for n in names)
    listed = [x for x in recs if x["type"] == "clip"]
    # contiguous, and each clip as long as the file really is
    assert listed[0]["start_sample"] == 0
    assert all(b["start_sample"] == a["end_sample"] for a, b in zip(listed, listed[1:]))
    for c in listed:
        assert c["end_sample"] - c["start_sample"] == opus_samples(tmp_path / "audio" / c["file"])
    assert all(abs(c["end_sample"] - c["start_sample"] - 48000) <= 960 for c in listed[:-1])
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels",
         "-of", "json", str(tmp_path / "audio" / names[-1])],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream == {"codec_name": "opus", "channels": 1}


def test_a_second_stop_request_does_not_signal_ffmpeg_again(tmp_path, monkeypatch):
    monkeypatch.setattr(rec_mod, "ANCHOR_WAIT_S", 0.2)

    class Slow(FakeFfmpeg):
        def send_signal(self, sig):
            self.signals.append(sig)  # keeps running until the second poll below

        def poll(self):
            if self.signals and self.returncode is None:
                self._clip()
                self.returncode = 255
            return self.returncode

    made = []
    r = Recorder(tmp_path, AUDIO, REC, popen=lambda argv, **kw: made.append(Slow(argv)) or made[-1],
                 sleep=lambda s: time.sleep(0.001))

    def stop_twice():
        r.request_stop()
        r.request_stop()

    threading.Timer(0.05, stop_twice).start()
    r.run()
    assert made[0].signals == [signal.SIGINT]


def ogg_opus(granule: int, preskip: int = 312) -> bytes:
    head = b"OggS" + bytes(2) + (0).to_bytes(8, "little") + bytes(16) + b"OpusHead" + bytes([1, 1]) \
        + preskip.to_bytes(2, "little") + bytes(8)
    return head + b"..." + b"OggS" + bytes(2) + granule.to_bytes(8, "little") + bytes(16)


def test_opus_samples_from_the_last_granule(tmp_path):
    p = tmp_path / "c.opus"
    p.write_bytes(ogg_opus(1440312))
    assert opus_samples(p) == 1440000
    p.write_bytes(b"OggS partial")
    assert opus_samples(p) is None
    assert opus_samples(tmp_path / "missing.opus") is None


def test_offsets_are_contiguous_even_when_ffmpeg_times_jitter(tmp_path, monkeypatch):
    monkeypatch.setattr(rec_mod, "ANCHOR_WAIT_S", 0.2)

    class Jitter(FakeFfmpeg):
        def _clip(self):
            name = Path(self.pattern % self._n).name
            Path(self.pattern % self._n).write_bytes(ogg_opus(1440000 + 312 + self._n))
            with self.csv.open("a") as f:  # the next clip's start is a few samples off
                f.write(f"{name},{(self._n * 1440003) / 48000:.6f},{((self._n + 1) * 1440000) / 48000:.6f}\n")
            self._n += 1

    r = Recorder(tmp_path, AUDIO, REC, popen=lambda argv, **kw: Jitter(argv, clips_before_stop=3),
                 sleep=lambda s: time.sleep(0.001))
    threading.Timer(0.05, r.request_stop).start()
    r.run()
    listed = [x for x in r.manifest.records() if x["type"] == "clip"]
    assert [(c["start_sample"], c["end_sample"]) for c in listed] == [
        (0, 1440000), (1440000, 2880001), (2880001, 4320003), (4320003, 5760006)]
