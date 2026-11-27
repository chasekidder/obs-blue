from stream_pilot.scenes import resolve_scene

OBS = ["Main Camera", "Outro", "Holding", "Jewelry Camera"]


def test_exact_name_wins():
    assert resolve_scene("Main Camera", OBS) == "Main Camera"


def test_case_and_spacing_do_not_matter():
    assert resolve_scene("main  camera ", OBS) == "Main Camera"


def test_a_renamed_scene_within_two_edits_is_found():
    assert resolve_scene("Jewlery Camera", OBS) == "Jewelry Camera"  # 2026-11-26


def test_no_guess_when_nothing_or_several_are_close():
    assert resolve_scene("Close-up", OBS) is None
    assert resolve_scene("Cam 1", ["Cam 2", "Cam 3"]) is None
    assert resolve_scene("Main Camera", []) is None
