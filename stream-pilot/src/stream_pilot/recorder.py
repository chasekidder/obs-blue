"""Microphone recorder: ``stream-pilot record <stream_dir>``.

Runs as ``stream-pilot-recorder@<stream dir>.service`` (a user unit of its own,
so a stream-pilot crash or restart never stops it). It records the configured
``[audio] mic_device`` straight from PipeWire/PulseAudio with ffmpeg, as Ogg
Opus mono clips of ``clip_s`` seconds in ``<stream_dir>/audio/``.

``audio/manifest.jsonl`` (one JSON object per line, appended and fsynced):

* ``{"type": "session", "session": 1, "started_at": <unix s>, "anchor": "pulse", ...}``:
  ``started_at`` is the wall-clock time of the session's first sample (the
  PulseAudio input's start timestamp; ``"anchor": "process_start"`` if ffmpeg
  did not report one).
* ``{"type": "clip", "session": 1, "file": "s01-0000.opus", "start_sample": 0,
  "end_sample": 1440000, ...}``: offsets in samples at 48 kHz from the session's
  first sample. Each clip starts where the previous one ended, and its length is
  the exact sample count of the file (its last Ogg page's granule position minus
  the Opus pre-skip); ffmpeg's clip times carry the PulseAudio timestamps' jitter
  of a few samples. A clip is listed once ffmpeg has closed it; only listed clips
  are finished (a clip cut short by a crash or power cut is not).
* ``{"type": "gap", "session": 2, "from": <unix s>, "to": <unix s>}``: no audio
  between the end of the previous session's last clip and this session's start.
* ``{"type": "session_end", "session": 1, "at": <unix s>, "reason": "..."}``.

A new session starts when ffmpeg exits on its own (for example the microphone
was unplugged) and when the unit is started again for the same stream (after a
resume). SIGINT or SIGTERM stops ffmpeg with SIGINT so it finalizes the last
clip, which is then listed before the recorder exits.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import AudioCfg, RecordingCfg

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
MANIFEST = "manifest.jsonl"
UNIT_TEMPLATE = "stream-pilot-recorder@.service"
# ffmpeg needs well under a second to close the last clip after SIGINT.
FFMPEG_STOP_WAIT_S = 6.0
ANCHOR_WAIT_S = 5.0
POLL_S = 0.5
RESTART_BACKOFF_S = 5.0

_START_RE = re.compile(r"\bstart: (\d+\.\d+)")


def clip_name(session: int, index: int | None = None) -> str:
    return f"s{session:02d}-{'%04d' if index is None else f'{index:04d}'}.opus"


def ffmpeg_argv(mic_device: str, audio_dir: Path, session: int, rec: RecordingCfg) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
        "-f", "pulse", "-sample_rate", str(SAMPLE_RATE), "-channels", "1",
        "-name", "stream-pilot-recorder", "-i", mic_device,
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-c:a", "libopus", "-b:a", f"{rec.bitrate_kbps}k", "-application", "voip",
        "-f", "segment", "-segment_time", str(rec.clip_s), "-segment_format", "opus",
        "-segment_list", str(audio_dir / f".s{session:02d}.csv"),
        "-segment_list_type", "csv", "-reset_timestamps", "1",
        str(audio_dir / clip_name(session)),
    ]


def parse_start(line: str) -> float | None:
    """Wall-clock start of the PulseAudio input from ffmpeg's ``start: <s>`` line."""
    m = _START_RE.search(line)
    if not m:
        return None
    value = float(m.group(1))
    return value if value > 1e9 else None  # a relative start (0.0) is no anchor


def opus_samples(path: Path) -> int | None:
    """Exact sample count of a finished Ogg Opus file: last granule position minus pre-skip."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    head, last = data.find(b"OpusHead"), data.rfind(b"OggS")
    if head < 0 or last < 0 or len(data) < last + 14 or len(data) < head + 12:
        return None
    preskip = int.from_bytes(data[head + 10:head + 12], "little")
    granule = int.from_bytes(data[last + 6:last + 14], "little", signed=True)
    return granule - preskip if granule > preskip else None


def parse_segment_line(line: str) -> tuple[str, float, float] | None:
    """``s01-0000.opus,0.000000,30.000000`` -> (file, start_s, end_s)."""
    parts = line.strip().split(",")
    if len(parts) != 3:
        return None
    try:
        return parts[0], float(parts[1]), float(parts[2])
    except ValueError:
        return None


class Manifest:
    def __init__(self, path: Path):
        self.path = Path(path)

    def records(self) -> list[dict]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        out = []
        for raw in text.split("\n"):
            try:
                rec = json.loads(raw)
            except ValueError:
                continue  # an unterminated last line after a power cut
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
        with self.path.open("a+b") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    data = b"\n" + data  # never glue onto a line cut short by a power cut
            f.write(data)
            f.flush()
            os.fsync(f.fileno())


def last_session(records: list[dict]) -> tuple[int, float | None]:
    """(highest session number, wall-clock end of its last listed clip, else its start)."""
    session = max((r["session"] for r in records if isinstance(r.get("session"), int)), default=0)
    start = next(
        (float(r["started_at"]) for r in records
         if r.get("type") == "session" and r.get("session") == session
         and isinstance(r.get("started_at"), (int, float))),
        None,
    )
    if start is None:
        return session, None
    ends = [r["end_sample"] for r in records
            if r.get("type") == "clip" and r.get("session") == session
            and isinstance(r.get("end_sample"), int)]
    return session, start + (max(ends) / SAMPLE_RATE if ends else 0.0)


def clips(records: list[dict]) -> list[str]:
    """Names of the finished clips (the only ones that may be copied off the PC)."""
    return [r["file"] for r in records if r.get("type") == "clip" and isinstance(r.get("file"), str)]


class Recorder:
    """One recorder process: sessions of ffmpeg until stopped."""

    def __init__(
        self,
        stream_dir: Path,
        audio: AudioCfg,
        rec: RecordingCfg,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.audio_dir = Path(stream_dir) / "audio"
        self.manifest = Manifest(self.audio_dir / MANIFEST)
        self.audio = audio
        self.rec = rec
        self._popen = popen
        self._clock = clock
        self._sleep = sleep
        self._stop = threading.Event()
        self._proc: Any = None
        self._interrupted: Any = None
        self._next_sample = 0

    def request_stop(self, *_args: object) -> None:
        self._stop.set()
        proc = self._proc
        # ffmpeg exits at once, without finishing the clip, on a second signal.
        if proc is not None and proc.poll() is None and proc is not self._interrupted:
            self._interrupted = proc
            try:
                proc.send_signal(signal.SIGINT)
            except OSError:
                pass

    def run(self) -> int:
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        while not self._stop.is_set():
            reason = self._session()
            if self._stop.is_set():
                break
            log.warning("recorder: %s; starting a new session in %.0fs", reason, RESTART_BACKOFF_S)
            self._stop.wait(RESTART_BACKOFF_S)
        return 0

    def _session(self) -> str:
        prev, prev_end = last_session(self.manifest.records())
        session = prev + 1
        argv = ffmpeg_argv(self.audio.mic_device, self.audio_dir, session, self.rec)
        csv_path = self.audio_dir / f".s{session:02d}.csv"
        launched = self._clock()
        try:
            self._proc = proc = self._popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True, errors="replace",
            )
        except OSError as exc:
            return f"cannot start ffmpeg: {exc}"
        if self._stop.is_set():
            self.request_stop()
        anchor: dict[str, float | None] = {"t": None}
        got_anchor = threading.Event()
        reader = threading.Thread(
            target=self._read_stderr, args=(proc, anchor, got_anchor), daemon=True
        )
        reader.start()
        got_anchor.wait(ANCHOR_WAIT_S)
        started_at = anchor["t"]
        kind = "pulse"
        if started_at is None:
            started_at, kind = launched, "process_start"
        if prev and prev_end is not None:
            self.manifest.append({"type": "gap", "session": session, "from": prev_end,
                                  "to": started_at})
        self.manifest.append({
            "type": "session", "session": session, "started_at": started_at, "anchor": kind,
            "mic_device": self.audio.mic_device, "sample_rate": SAMPLE_RATE, "channels": 1,
            "bitrate_kbps": self.rec.bitrate_kbps, "clip_s": self.rec.clip_s,
        })
        log.info("recorder: session %d started (%s anchor)", session, kind)
        offset = 0
        self._next_sample = 0
        stop_sent_at: float | None = None
        while True:
            offset = self._list_clips(csv_path, offset, session, started_at)
            code = proc.poll()
            if code is not None:
                break
            if self._stop.is_set():
                stop_sent_at = stop_sent_at or self._clock()
                if self._clock() - stop_sent_at > FFMPEG_STOP_WAIT_S:
                    log.warning("recorder: ffmpeg did not stop; killing it")
                    proc.kill()
            self._sleep(POLL_S)
        reader.join(1.0)
        self._list_clips(csv_path, offset, session, started_at)
        reason = "stopped" if self._stop.is_set() else f"ffmpeg exited with status {code}"
        self.manifest.append({"type": "session_end", "session": session, "at": self._clock(),
                              "reason": reason})
        self._proc = None
        return reason

    def _read_stderr(self, proc: Any, anchor: dict, got: threading.Event) -> None:
        try:
            for line in proc.stderr:
                if anchor["t"] is None:
                    t = parse_start(line)
                    if t is not None:
                        anchor["t"] = t
                        got.set()
                if "rror" in line or "fail" in line.lower():
                    log.warning("ffmpeg: %s", line.rstrip())
        except (OSError, ValueError):
            pass
        finally:
            got.set()

    def _list_clips(self, csv_path: Path, offset: int, session: int, started_at: float) -> int:
        try:
            with csv_path.open("rb") as f:
                f.seek(offset)
                data = f.read()
        except FileNotFoundError:
            return offset
        complete = data.rfind(b"\n") + 1
        for raw in data[:complete].decode(errors="replace").splitlines():
            seg = parse_segment_line(raw)
            if seg is None:
                continue
            name, start_s, end_s = seg
            samples = opus_samples(self.audio_dir / name)
            if samples is None:
                samples = round(end_s * SAMPLE_RATE) - round(start_s * SAMPLE_RATE)
            start = self._next_sample
            end = self._next_sample = start + samples
            self.manifest.append({
                "type": "clip", "session": session, "file": name,
                "start_sample": start, "end_sample": end,
                "start_at": started_at + start / SAMPLE_RATE,
            })
        return offset + complete


def run_recorder(stream_dir: Path, audio: AudioCfg, rec: RecordingCfg) -> int:
    recorder = Recorder(stream_dir, audio, rec)
    signal.signal(signal.SIGINT, recorder.request_stop)
    signal.signal(signal.SIGTERM, recorder.request_stop)
    return recorder.run()
