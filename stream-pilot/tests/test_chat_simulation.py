"""Chat simulation: replay whole streams' worth of comments into a copy of the 2026
dashboard Comments module and check that the DOM capture logs every comment exactly
once, in order, with its author, text and Facebook id.

The schedule mixes steady chat, bursts (a comment every 15-60 ms), fast runs and pauses.
The page relabels comment times as they age, re-renders the whole list now and then, and
(in one variant) keeps only the newest comments in the DOM.

Transcripts: a synthetic resale-group chat always runs. Real ones (comments of past
lives, which name real people and so never enter the repo) run when
STREAM_PILOT_TRANSCRIPTS points at a directory of JSON files ([{author, text,
fb_comment_id, reply_to}], oldest first). STREAM_PILOT_SIM_SPEED > 1 replays faster.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path

import pytest

from stream_pilot.comment_log import CommentLog
from stream_pilot.fb import selectors as S
from stream_pilot.fb.comments import FbDomCapture

LIVE_ID = "1122334455667788"
SPEED = float(os.environ.get("STREAM_PILOT_SIM_SPEED", "1"))

_NAMES = [
    "Brenda Tucker", "Zoë O'Neil-Smith", "José Álvarez", "Mary Ann Kowalczyk", "Lin Chen",
    "Debbie Ross", "Tammy Jo Fischer", "Karen", "Patricia van der Berg", "Aaliyah Johnson",
    "Barb Miller", "Sue Ellen Hart", "Nguyễn Thị Lan", "Ray", "Cindy Lou Who",
]
_TEXTS = [
    "35 🙏🏻🙏🏻", "29 coke x2 5", "#31 $3", "sold", "Sold!", "sold sold", "SOLD 🙌", "mine", "claim", "#12", "12 please", "I'll take 4 and 7",
    "how much?", "price on the blue one?", "Is the lamp still available?", "😀", "❤️❤️❤️", "👍",
    "Can I pick up tomorrow after 5?", "ship to 49503?", "what size is that?", "<3 this",
    "Tom & Jerry mugs?", 'the "vintage" one', "https://example.com/item/42", "lol",
    "ok   spaces    collapse", "Line one\nline two",
    "This is a much longer comment, the kind somebody types while waiting for the next item to "
    "come up, asking about measurements, condition, and whether it can be held until Friday.",
]


def synthetic_chat(n: int = 320, seed: int = 7) -> list[dict]:
    rnd = random.Random(seed)
    out: list[dict] = []
    next_id = 2426900000000000
    for _ in range(n):
        next_id += rnd.randint(1, 900)
        author = rnd.choice(_NAMES)
        r = rnd.random()
        if out and r < 0.06:  # the same person repeats their last comment (a second claim)
            prev = next((c for c in reversed(out) if c["author"] == author), None)
            text = prev["text"] if prev else "sold"
        else:
            text = rnd.choice(_TEXTS)
        reply_to = None
        if out and rnd.random() < 0.08:
            reply_to = rnd.choice([c for c in out if not c["reply_to"]] or out)["fb_comment_id"]
        out.append({"author": author, "text": text, "fb_comment_id": str(next_id), "reply_to": reply_to})
    return out


def real_transcripts() -> list[Path]:
    d = os.environ.get("STREAM_PILOT_TRANSCRIPTS")
    return sorted(Path(d).glob("*.json")) if d else []


def prepare(transcript: list[dict]) -> list[dict]:
    """Ids for comments without one; a reply only after its parent; drop empty entries."""
    out, seen = [], set()
    pending: dict[str, list[dict]] = {}
    fake = 1
    for c in transcript:
        c = dict(c)
        if not c.get("fb_comment_id"):
            c["fb_comment_id"] = f"9000000000{fake:06d}"
            fake += 1
        if not c.get("author"):
            continue
        parent = c.get("reply_to")
        if parent and parent not in seen and any(x.get("fb_comment_id") == parent for x in transcript):
            pending.setdefault(parent, []).append(c)
            continue
        if parent and parent not in seen:
            c["reply_to"] = None
        for x in [c] + pending.pop(c["fb_comment_id"], []):
            out.append(x)
            seen.add(x["fb_comment_id"])
    return out


def schedule(transcript: list[dict], seed: int = 1) -> list[dict]:
    """Delays in phases: steady, burst, fast, pause."""
    rnd = random.Random(seed)
    phases = [
        (lambda: rnd.randint(300, 1200), 15),   # steady chat
        (lambda: rnd.randint(15, 60), 30),      # a burst ("sold" storm)
        (lambda: rnd.randint(100, 300), 20),    # fast
        (lambda: rnd.randint(2000, 3500), 2),   # a pause
    ]
    out, i, p = [], 0, 0
    while i < len(transcript):
        delay, count = phases[p % len(phases)]
        for _ in range(count):
            if i >= len(transcript):
                break
            c = transcript[i]
            out.append({
                "delay_ms": max(1, int(delay() / SPEED)),
                "author": c["author"], "text": c["text"],
                "id": c["fb_comment_id"], "reply_to": c.get("reply_to"),
            })
            i += 1
        p += 1
    return out


def fb_dom_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [r for r in map(json.loads, path.read_text().splitlines()) if r.get("source") == "fb_dom"]


def explain(got: list[tuple], want: list[tuple]) -> str:
    got_ids = [g[0] for g in got]
    want_ids = [w[0] for w in want]
    missing = [w for w in want if w[0] not in set(got_ids)]
    dupes = sorted({i for i in got_ids if got_ids.count(i) > 1})
    wrong = [(w, g) for w in want for g in got if g[0] == w[0] and g != w]
    order_ok = [g for g in got_ids if g in set(want_ids)] == [w for w in want_ids if w in set(got_ids)]
    return (f"logged {len(got)} of {len(want)}; missing {len(missing)} {missing[:5]}; "
            f"duplicated ids {dupes[:5]}; wrong author/text {wrong[:5]}; order ok: {order_ok}")


async def replay(pw_browser, fixture_base_url, tmp_path, transcript, **opts):
    sched = schedule(prepare(transcript))
    context = await pw_browser.new_context()
    try:
        page = await context.new_page()
        log = CommentLog(tmp_path / "comments.jsonl", LIVE_ID)
        cap = FbDomCapture(context)
        await cap.attach(page, log)
        url = f"{fixture_base_url}/comments_module_sim.html{S.popout_url_fragment(LIVE_ID)}"
        await page.goto(url)
        await page.wait_for_function("window.__spWatcherInstalled === true")
        await page.evaluate("([s, o]) => window.__simRun(s, o)", [sched, opts])
        budget = sum(s["delay_ms"] for s in sched) / 1000 + 30
        await page.wait_for_function("window.__simDone === true", timeout=budget * 1000)
        await asyncio.sleep(1.5)  # > the watcher's debounce, after the final re-render
        want = [(s["id"], s["author"], " ".join(s["text"].split())) for s in sched]
        got = [(r["fb_comment_id"], r["author"], r["text"]) for r in fb_dom_rows(log.path)]
        assert got == want, explain(got, want)
        assert log.gaps == []
        await cap.detach()
    finally:
        await context.close()


@pytest.mark.browser
async def test_synthetic_chat_is_logged_completely(pw_browser, fixture_base_url, tmp_path):
    await replay(pw_browser, fixture_base_url, tmp_path, synthetic_chat(),
                 relabelMs=4000, rerenderEvery=25)


@pytest.mark.browser
async def test_synthetic_chat_with_only_the_newest_comments_in_the_dom(pw_browser, fixture_base_url, tmp_path):
    await replay(pw_browser, fixture_base_url, tmp_path, synthetic_chat(seed=11),
                 relabelMs=3000, cap=40)


@pytest.mark.browser
@pytest.mark.parametrize("path", real_transcripts(), ids=lambda p: p.stem)
async def test_real_stream_transcript_is_logged_completely(pw_browser, fixture_base_url, tmp_path, path):
    transcript = json.loads(path.read_text())
    started = time.monotonic()
    await replay(pw_browser, fixture_base_url, tmp_path, transcript, relabelMs=4000, rerenderEvery=40)
    print(f"{path.stem}: {len(transcript)} comments replayed in {time.monotonic() - started:.0f}s")


def test_synthetic_chat_covers_the_hard_cases():
    chat = synthetic_chat()
    texts = [c["text"] for c in chat]
    assert any(c["reply_to"] for c in chat)
    assert any(t == "😀" for t in texts) and any("\n" in t for t in texts)
    assert any(a["author"] == b["author"] and a["text"] == b["text"] for a, b in zip(chat, chat[1:])) or \
        len({(c["author"], c["text"]) for c in chat}) < len(chat)
    sched = schedule(prepare(chat))
    assert min(s["delay_ms"] for s in sched) <= 60 * max(1, 1 / SPEED)  # bursts
    assert max(s["delay_ms"] for s in sched) >= 2000 / SPEED  # pauses
    assert len({s["id"] for s in sched}) == len(sched)


def test_prepare_moves_a_reply_after_its_parent_and_fills_missing_ids():
    t = [
        {"author": "B", "text": "reply", "fb_comment_id": "2", "reply_to": "1"},
        {"author": "A", "text": "parent", "fb_comment_id": "1", "reply_to": None},
        {"author": "C", "text": "no id", "fb_comment_id": None, "reply_to": None},
        {"author": "D", "text": "orphan", "fb_comment_id": "4", "reply_to": "999"},
    ]
    out = prepare(t)
    assert [c["text"] for c in out] == ["parent", "reply", "no id", "orphan"]
    assert out[2]["fb_comment_id"] and out[3]["reply_to"] is None
