import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically: tmp file in the same dir, fsync, os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


@dataclass
class Saved:
    state: str
    live_id: str | None = None
    stream_dir: str | None = None


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Saved | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as e:
            log.warning("cannot read %s: %s", self.path, e)
            return None
        try:
            data = json.loads(raw)
        except ValueError as e:
            log.warning("corrupt state file %s: %s", self.path, e)
            return None
        if not isinstance(data, dict) or not isinstance(data.get("state"), str):
            log.warning("unexpected shape in state file %s", self.path)
            return None
        live_id = data.get("live_id")
        stream_dir = data.get("stream_dir")
        if not (live_id is None or isinstance(live_id, str)) or not (
            stream_dir is None or isinstance(stream_dir, str)
        ):
            log.warning("unexpected field types in state file %s", self.path)
            return None
        return Saved(state=data["state"], live_id=live_id, stream_dir=stream_dir)

    def save(self, saved: Saved) -> None:
        _atomic_write_json(self.path, asdict(saved))


class StatusWriter:
    def __init__(self, path: Path):
        self.path = Path(path)

    def write(
        self,
        *,
        state: str,
        since: float,
        last_error: str | None,
        live_id: str | None,
        memory: dict | None,
        features: dict[str, bool] | None = None,
        paused: bool = False,
    ) -> None:
        data = {
            "state": state,
            "since": since,
            "last_error": last_error,
            "live_id": live_id,
            "memory": memory,
            "updated_at": time.time(),
        }
        # Optional (readers must not require it): older images ignore it.
        if features is not None:
            data["features"] = features
        if paused:
            data["paused"] = True
        _atomic_write_json(self.path, data)
