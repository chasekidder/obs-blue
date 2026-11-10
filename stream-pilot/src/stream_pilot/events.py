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


Event = DeckPress | PowerPress | ObsStreamState | RetryRequested | Tick
