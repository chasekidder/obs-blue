import json

from stream_pilot.display import mirror_args, run_display_mirror


def out(id_, name, *, enabled=True, connected=True, priority=0, source=0, x=0, y=0):
    return {
        "id": id_, "name": name, "enabled": enabled, "connected": connected,
        "priority": priority, "replicationSource": source, "pos": {"x": x, "y": y},
    }


def test_extended_displays_are_mirrored_onto_the_primary_at_the_origin():
    # Seen on the stream PC: the NanoKVM (primary) was extended to the right of a TV.
    outputs = [out(1, "HDMI-A-2", priority=1, x=1920), out(2, "HDMI-A-1", priority=0)]
    assert mirror_args(outputs) == ["output.HDMI-A-2.position.0,0", "output.HDMI-A-1.mirror.HDMI-A-2"]


def test_already_mirrored_needs_nothing():
    outputs = [out(1, "HDMI-A-2", priority=1), out(2, "HDMI-A-1", source=1)]
    assert mirror_args(outputs) == []


def test_an_existing_mirror_is_kept_even_when_the_replica_has_priority_1():
    # Seen on the stream PC: after mirroring, KWin gave the replica priority 1. Choosing
    # it as the source made both outputs mirror each other (no outputs at all).
    outputs = [out(1, "HDMI-A-2", priority=2), out(2, "HDMI-A-1", priority=1, source=1)]
    assert mirror_args(outputs) == []


def test_a_mirror_cycle_is_broken_at_the_primary():
    outputs = [out(1, "HDMI-A-2", priority=2, source=2), out(2, "HDMI-A-1", priority=1, source=1)]
    assert mirror_args(outputs) == ["output.HDMI-A-1.mirror.none"]


def test_a_single_display_needs_nothing():
    assert mirror_args([out(1, "HDMI-A-1", priority=1, x=1920)]) == []
    assert mirror_args([out(1, "HDMI-A-1", priority=1), out(2, "DP-1", connected=False)]) == []


def test_a_connected_but_disabled_display_is_enabled_and_mirrored():
    outputs = [out(1, "HDMI-A-1", priority=1), out(2, "HDMI-A-2", enabled=False)]
    assert mirror_args(outputs) == ["output.HDMI-A-2.enable", "output.HDMI-A-2.mirror.HDMI-A-1"]


def test_without_a_primary_the_lowest_id_is_the_source():
    outputs = [out(3, "DP-2"), out(2, "DP-1")]
    assert mirror_args(outputs) == ["output.DP-2.mirror.DP-1"]


def test_the_primary_is_the_lowest_positive_priority():
    outputs = [out(1, "A", priority=2), out(2, "B", priority=1), out(3, "C", priority=0)]
    assert mirror_args(outputs) == ["output.A.mirror.B", "output.C.mirror.B"]


def test_the_loop_applies_mirroring_only_when_needed_and_survives_errors():
    states = [
        RuntimeError("no Wayland session yet"),
        {"outputs": [out(1, "A", priority=1, x=1920), out(2, "B")]},
        {"outputs": [out(1, "A", priority=1), out(2, "B", source=1)]},
    ]
    calls = []

    def kscreen(*args):
        calls.append(args)
        if args == ("-j",):
            s = states.pop(0)
            if isinstance(s, Exception):
                raise s
            return json.dumps(s)
        return ""

    assert run_display_mirror(kscreen=kscreen, sleep=lambda s: None, rounds=3) == 0
    assert calls == [("-j",), ("-j",), ("output.A.position.0,0", "output.B.mirror.A"), ("-j",)]


def test_a_replica_of_the_wrong_source_is_repointed():
    outputs = [out(1, "A", priority=1), out(2, "B", source=3), out(3, "C", source=0, priority=2)]
    assert mirror_args(outputs) == ["output.B.mirror.A", "output.C.mirror.A"]
