"""Layered TOML configuration for stream-pilot.

The packaged ``data/default-config.toml`` is always loaded first; the files
passed to :func:`load_config` are merged on top in order (later wins, nested
tables merged key by key). Missing files are skipped; unknown keys and
unreadable files are logged and ignored so a typo in ``site.toml`` can never
stop the controller.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, fields
from importlib import resources
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_PATHS = [
    Path("/etc/stream-pilot/config.toml"),
    Path("~/.config/stream-pilot/site.toml").expanduser(),
    Path("~/.config/stream-pilot/secrets.toml").expanduser(),
]


@dataclass(frozen=True)
class FacebookCfg:
    actor_id: str
    group_id: str
    group_name: str
    producer_url_template: str
    comments_url_template: str
    live_title_template: str

    @property
    def producer_url(self) -> str:
        return self.producer_url_template.format(
            actor_id=self.actor_id, group_id=self.group_id
        )


@dataclass(frozen=True)
class ObsCfg:
    host: str
    port: int
    password: str


@dataclass(frozen=True)
class CdpCfg:
    url: str
    user_data_dir: Path
    chromium_bin: str


@dataclass(frozen=True)
class SsnCfg:
    enabled: bool
    session_id: str


@dataclass(frozen=True)
class CameraCfg:
    scene: str
    device: str


@dataclass(frozen=True)
class AudioCfg:
    mic_device: str


@dataclass(frozen=True)
class RecordingCfg:
    enabled: bool
    bitrate_kbps: int
    clip_s: int
    fb_delay_s: float
    replay_includes_preroll: bool


@dataclass(frozen=True)
class UploadCfg:
    enabled: bool
    target: str
    host_key: str
    port: int
    bwlimit_kbps: int
    timeout_s: int
    connect_timeout_s: int
    final_timeout_s: float
    retention_days: float


@dataclass(frozen=True)
class HoldingCfg:
    enabled: bool
    mode: str
    text: str


@dataclass(frozen=True)
class BrandingCfg:
    enabled: bool
    watermark_text: str
    watermark_corner: str
    outro_text: str
    outro_s: float


@dataclass(frozen=True)
class WbFreezeCfg:
    enabled: bool
    camera: str
    freeze_exposure: bool
    rebalance_s: float


@dataclass(frozen=True)
class AudioFiltersCfg:
    enabled: bool
    limiter_db: float


@dataclass(frozen=True)
class MemoryCfg:
    interval_s: int
    floor_mb: int
    popout_rss_warn_mb: int
    auto_reload_popout: bool


@dataclass(frozen=True)
class DeckCfg:
    long_press_s: float
    brightness: int


@dataclass(frozen=True)
class TimeoutsCfg:
    step_s: float
    attempts: int
    preview_s: float
    live_confirm_s: float
    stop_s: float
    fb_end_wait_s: float
    net_retry_s: float
    reconnect_retry_s: float


@dataclass(frozen=True)
class PathsCfg:
    state_dir: Path
    streams_dir: Path
    failures_dir: Path


# Config sections that are feature switches: each has a boolean ``enabled``
# (shipped off, except ssn) and is listed by `stream-pilot status`.
FEATURE_SECTIONS: tuple[str, ...] = (
    "ssn", "recording", "upload", "holding", "branding", "wb_freeze", "audio_filters",
)


@dataclass(frozen=True)
class Config:
    facebook: FacebookCfg
    obs: ObsCfg
    cdp: CdpCfg
    ssn: SsnCfg
    cameras: dict[str, CameraCfg]
    audio: AudioCfg
    memory: MemoryCfg
    deck: DeckCfg
    timeouts: TimeoutsCfg
    paths: PathsCfg
    recording: RecordingCfg
    upload: UploadCfg
    holding: HoldingCfg
    branding: BrandingCfg
    wb_freeze: WbFreezeCfg
    audio_filters: AudioFiltersCfg

    def features(self) -> dict[str, bool]:
        """Every feature switch (``[<name>] enabled``), in FEATURE_SECTIONS order."""
        return {name: bool(getattr(self, name).enabled) for name in FEATURE_SECTIONS}

    def missing_site_values(self) -> list[str]:
        """Names of required site values that are empty (whitespace counts)."""
        required = {
            "facebook.actor_id": self.facebook.actor_id,
            "facebook.group_id": self.facebook.group_id,
            "facebook.group_name": self.facebook.group_name,
        }
        return [name for name, value in required.items() if not str(value).strip()]


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


def _read_toml(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("ignoring unreadable config file %s: %s", path, exc)
        return None


def _default_data() -> dict[str, Any]:
    text = (resources.files("stream_pilot") / "data/default-config.toml").read_text(
        encoding="utf-8"
    )
    return tomllib.loads(text)


def _build(cls: type, where: str, data: Any, defaults: Any) -> Any:
    """Build dataclass ``cls`` from ``data``, ignoring (and logging) unknown keys.

    ``defaults`` supplies values for keys missing from ``data`` (used only if a
    section was replaced by a non-table value in a user file).
    """
    if not isinstance(data, dict):
        log.warning("config section [%s] is not a table; using defaults", where)
        data = {}
    known = {f.name: f for f in fields(cls)}
    for key in data:
        if key not in known:
            log.warning("ignoring unknown config key %s.%s", where, key)
    kwargs: dict[str, Any] = {}
    for name in known:
        val = data.get(name, defaults[name])
        if name == "enabled" and not isinstance(val, bool):
            log.warning("config %s.enabled must be true or false, not %r; using %s",
                        where, val, str(defaults[name]).lower())
            val = defaults[name]
        kwargs[name] = _coerce(name, val)
    return cls(**kwargs)


_PATH_FIELDS = {"user_data_dir", "state_dir", "streams_dir", "failures_dir"}


def _coerce(name: str, val: Any) -> Any:
    if name in _PATH_FIELDS:
        return Path(str(val)).expanduser()
    return val


def load_config(paths: list[Path]) -> Config:
    data = _default_data()
    pristine = _default_data()
    for path in paths:
        layer = _read_toml(Path(path))
        if layer is not None:
            data = _merge(data, layer)

    sections: dict[str, type] = {
        "facebook": FacebookCfg,
        "obs": ObsCfg,
        "cdp": CdpCfg,
        "ssn": SsnCfg,
        "audio": AudioCfg,
        "memory": MemoryCfg,
        "deck": DeckCfg,
        "timeouts": TimeoutsCfg,
        "paths": PathsCfg,
        "recording": RecordingCfg,
        "upload": UploadCfg,
        "holding": HoldingCfg,
        "branding": BrandingCfg,
        "wb_freeze": WbFreezeCfg,
        "audio_filters": AudioFiltersCfg,
    }
    for key in data:
        if key not in sections and key != "cameras":
            log.warning("ignoring unknown config section [%s]", key)

    built = {
        name: _build(cls, name, data.get(name), pristine[name])
        for name, cls in sections.items()
    }

    cams_raw = data.get("cameras")
    if not isinstance(cams_raw, dict):
        log.warning("config section [cameras] is not a table; using defaults")
        cams_raw = pristine["cameras"]
    cameras: dict[str, CameraCfg] = {}
    for cam, cam_data in cams_raw.items():
        base = pristine["cameras"].get(cam, {"scene": "", "device": ""})
        cameras[cam] = _build(CameraCfg, f"cameras.{cam}", cam_data, base)

    return Config(cameras=cameras, **built)
