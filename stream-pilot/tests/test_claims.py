import pytest

from stream_pilot.claims import looks_like_claim
from stream_pilot.fb.comments import render_watcher

CLAIMS = [
    "21 cookie sheets 3",
    "26blue 7",
    "$5 crew mens#31",
    "#31$house$3",
    "18 $6 x mas wreath",
    "55 4 teal",
    "6 resess X2 $3",
    "45 penguin gift tags 2/1.00",
    "10- hat 3.00",
    "45 penguin gift tags 2/1.00 and the matching ribbon set if it is still available please, thank you so much!",
]
CHAT = ["55 how much", "6 sure", "20 goodnight everyone", "Hi Penny", "", "   ",
        "Does anyone know if the blue vase from earlier is still available? I would love it for my "
        "daughter who just moved into her first apartment 🏠💙"]
# Guessing-game entries and bids may be highlighted; a false highlight is harmless.
MAY = ["21 40 miles", "55 5500"]


@pytest.mark.parametrize("text", CLAIMS)
def test_claims_are_highlighted(text):
    assert looks_like_claim(text)


@pytest.mark.parametrize("text", CHAT)
def test_chat_is_not(text):
    assert not looks_like_claim(text)


@pytest.mark.parametrize("text", MAY)
def test_games_and_bids_may_be(text):
    assert looks_like_claim(text)


def test_long_messages_are_not_claims():
    assert not looks_like_claim("21 " + "really " * 30 + "nice, I bought 3 last year")


def test_watcher_highlighting_is_off_unless_switched_on():
    assert "const CLAIM_HIGHLIGHT = false;" in render_watcher()
    assert "const CLAIM_HIGHLIGHT = true;" in render_watcher(claim_highlight=True)


def test_watcher_zoom_is_rendered_and_clamped():
    assert "const COMMENTS_ZOOM = 1;" in render_watcher()
    assert "const COMMENTS_ZOOM = 1.5;" in render_watcher(comments_zoom=1.5)
    assert "const COMMENTS_ZOOM = 3;" in render_watcher(comments_zoom=9)
    assert "const COMMENTS_ZOOM = 0.5;" in render_watcher(comments_zoom=0)
