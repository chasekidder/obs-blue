from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    STARTING = "STARTING"
    NO_NET = "NO_NET"
    LOGIN = "LOGIN"
    SETUP = "SETUP"
    READY = "READY"
    GOING_LIVE = "GOING_LIVE"
    LIVE = "LIVE"
    RECONNECTING = "RECONNECTING"
    ENDING = "ENDING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class KeyFace:
    label: str
    bg: str  # "#rrggbb"
    fg: str = "#ffffff"
    blink: bool = False


STATUS_FACES: dict[State, KeyFace] = {
    State.STARTING: KeyFace("BOOT", "#555555"),
    State.NO_NET: KeyFace("NO NET", "#555555"),
    State.LOGIN: KeyFace("LOG IN", "#e67e00"),
    State.SETUP: KeyFace("SETUP", "#1f5fbf"),
    State.READY: KeyFace("GO LIVE", "#1e9e3e"),
    State.GOING_LIVE: KeyFace("GOING\nLIVE", "#1f5fbf", blink=True),
    State.LIVE: KeyFace("LIVE", "#c0141c"),
    State.RECONNECTING: KeyFace("RECON-\nNECT", "#c0141c", blink=True),
    State.ENDING: KeyFace("ENDING", "#c0141c"),
    State.ERROR: KeyFace("ERROR", "#e67e00"),
}

LIVE_STATES = frozenset({State.LIVE, State.RECONNECTING})
