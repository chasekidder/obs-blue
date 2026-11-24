"""What a purchase claim looks like in the sale's comments (``[claim_highlight]``).

Claims are written ``<buyer id> <item words> <price>`` in many shapes: ``21
cookie sheets 3``, ``26blue 7``, ``$5 crew mens#31``, ``#31$house$3``, ``18 $6
x mas wreath``, ``45 penguin gift tags 2/1.00``, ``10- hat 3.00``. The rule:
the comment starts with a number (optionally after ``#`` or ``$``) and has at
least two numbers, and is short. Chat such as ``55 how much`` or ``6 sure``
has one number. Guessing-game entries (``21 40 miles``) and bids (``55 5500``)
match too; a false highlight is harmless.

The same patterns are rendered into fb/watcher.js, so Python and the page agree.
"""

from __future__ import annotations

import re

CLAIM_START_RE = re.compile(r"^\s*[#$]?\d")
# One number: digits, optionally with decimals or a fraction (3.00, 2/1.00, 1,50).
CLAIM_NUMBER_RE = re.compile(r"\d+(?:[.,/]\d+)*")
CLAIM_MAX_CHARS = 80
CLAIM_CLASS = "sp-claim"


def looks_like_claim(text: str) -> bool:
    text = (text or "").strip()
    if not text or len(text) > CLAIM_MAX_CHARS or not CLAIM_START_RE.match(text):
        return False
    return len(CLAIM_NUMBER_RE.findall(text)) >= 2
