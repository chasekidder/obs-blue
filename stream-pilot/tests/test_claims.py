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
]
CHAT = ["55 how much", "6 sure", "20 goodnight everyone", "Hi Penny", "", "   "]
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
    assert not looks_like_claim("21 " + "really " * 20 + "nice, I bought 3 last year")


def test_watcher_highlighting_is_off_unless_switched_on():
    assert "const CLAIM_HIGHLIGHT = false;" in render_watcher()
    assert "const CLAIM_HIGHLIGHT = true;" in render_watcher(claim_highlight=True)
