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
class DeckDown:
    """A Stream Deck key went down (its DeckPress follows on release)."""

    key: int


@dataclass(frozen=True)
class PauseRequested:
    """``stream-pilot pause`` / the desktop icon: toggle pause."""


@dataclass(frozen=True)
class EndRequested:
    """``stream-pilot press --long``: end a live at once (no confirmation press)."""


@dataclass(frozen=True)
class ProgramSceneChanged:
    """OBS's program scene changed (by stream-pilot or by the host in OBS)."""

    name: str


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
    | EndRequested | ProgramSceneChanged | DeckDown | PauseRequested
)
