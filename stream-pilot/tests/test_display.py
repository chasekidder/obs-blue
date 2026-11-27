import json

from stream_pilot.display import mirror_args, run_display_mirror


def out(id_, name, *, enabled=True, connected=True, priority=0, source=0, x=0, y=0, native=None):
    o = {
        "id": id_, "name": name, "enabled": enabled, "connected": connected,
        "priority": priority, "replicationSource": source, "pos": {"x": x, "y": y},
    }
    if native:
        o.update(modes=[{"id": "1", "size": {"width": native[0], "height": native[1]}}],
                 preferredModes=["1"], currentModeId="1")
    return o


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

    restarts = []
    assert run_display_mirror(kscreen=kscreen, sleep=lambda s: None, rounds=3,
                              keyboard_visible=lambda: False,
                              restart_keyboard=lambda: restarts.append(1)) == 0
    assert calls == [("-j",), ("-j",), ("output.A.position.0,0", "output.B.mirror.A"), ("-j",)]
    assert restarts == [1]  # the screens changed: the keyboard is laid out afresh


def test_a_replica_of_the_wrong_source_is_repointed():
    outputs = [out(1, "A", priority=1), out(2, "B", source=3), out(3, "C", source=0, priority=2)]
    assert mirror_args(outputs) == ["output.B.mirror.A", "output.C.mirror.A"]


KVM, DELL = (1920, 1080), (1600, 900)


def test_the_largest_display_is_the_source_even_after_it_came_back():
    # Seen in situ: the NanoKVM's HDMI dropped for a moment; when it came back the Dell
    # (1600x900) was the source, the desktop stayed 1600x900 and OBS no longer fit.
    outputs = [out(1, "HDMI-A-2", priority=2, source=2, native=KVM),
               out(2, "HDMI-A-1", priority=1, native=DELL)]
    assert mirror_args(outputs) == ["output.HDMI-A-2.mirror.none", "output.HDMI-A-1.mirror.HDMI-A-2"]


def test_the_kvm_as_source_with_the_monitor_mirroring_needs_nothing():
    outputs = [out(1, "HDMI-A-2", priority=1, native=KVM),
               out(2, "HDMI-A-1", priority=2, source=1, native=DELL)]
    assert mirror_args(outputs) == []


def test_a_freshly_connected_larger_display_becomes_the_source():
    outputs = [out(2, "HDMI-A-1", priority=1, native=DELL), out(1, "HDMI-A-2", native=KVM)]
    assert mirror_args(outputs) == ["output.HDMI-A-1.mirror.HDMI-A-2"]


def test_a_disabled_kvm_coming_back_becomes_the_source_in_one_pass():
    # In situ: the KVM's output came back disabled while the Dell was the only display.
    outputs = [out(1, "HDMI-A-2", priority=2, enabled=False, native=KVM),
               out(2, "HDMI-A-1", priority=1, native=DELL)]
    assert mirror_args(outputs) == ["output.HDMI-A-2.enable", "output.HDMI-A-1.mirror.HDMI-A-2"]


def _loop(states, visible):
    def kscreen(*args):
        return json.dumps(states.pop(0)) if args == ("-j",) else ""

    restarts = []
    run_display_mirror(kscreen=kscreen, sleep=lambda s: None, rounds=len(states),
                       keyboard_visible=lambda: visible.pop(0) if visible else False,
                       restart_keyboard=lambda: restarts.append(1))
    return restarts


def test_keyboard_not_restarted_while_the_screens_stay_the_same():
    same = {"outputs": [out(1, "A", priority=1), out(2, "B", source=1)]}
    assert _loop([same, same, same], []) == []


def test_keyboard_restarted_once_after_a_change_and_only_when_closed():
    a = {"outputs": [out(1, "A", priority=1), out(2, "B", source=1)]}
    b = {"outputs": [out(2, "B", priority=1)]}  # the KVM (A) is gone; B alone needs nothing
    # the change is seen in round 2; the keyboard is open in rounds 2-3, closed in 4
    assert _loop([a, b, b, b, b], [True, True, False]) == [1]


def test_restart_keyboard_toggles_the_input_method_setting():
    from stream_pilot.display import PLASMA_KEYBOARD, restart_keyboard

    calls = []

    def run(argv):
        calls.append(argv)
        return "" if argv[0] == "kreadconfig6" else ""

    assert restart_keyboard(run=run, sleep=lambda s: None) is True
    writes = [c[-1] for c in calls if c[0] == "kwriteconfig6"]
    assert writes == ["", PLASMA_KEYBOARD]
    assert all("--notify" in c for c in calls if c[0] == "kwriteconfig6")


def test_restart_keyboard_leaves_another_input_method_alone():
    from stream_pilot.display import restart_keyboard

    calls = []
    run = lambda argv: calls.append(argv) or ("/usr/share/applications/fcitx5.desktop" if argv[0] == "kreadconfig6" else "")
    assert restart_keyboard(run=run, sleep=lambda s: None) is False
    assert not [c for c in calls if c[0] == "kwriteconfig6"]
