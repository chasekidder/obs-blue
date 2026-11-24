from dataclasses import dataclass


@dataclass(frozen=True)
class DeckPress:
    key: int
    long: bool = False


@dataclass(frozen=True)
class PowerPress:
    pass


@dataclass(frozen=True)
class ObsStreamState:
    active: bool
    reconnecting: bool = False


@dataclass(frozen=True)
class RetryRequested:
    pass


@dataclass(frozen=True)
class Tick:
    pass


@dataclass(frozen=True)
class SsnStalled:
    """SSN posted that its background page stopped and it could not reopen it."""


@dataclass(frozen=True)
class ObsConnected:
    """obs-websocket (re)connected and identified (also after an OBS restart)."""


Event = (
    DeckPress | PowerPress | ObsStreamState | RetryRequested | Tick | SsnStalled | ObsConnected
)
