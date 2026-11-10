import json
from pathlib import Path

from stream_pilot.comment_log import Comment, CommentLog, build_report


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def C(author: str, text: str, fb_id: str | None = None) -> Comment:
    return Comment(author=author, text=text, fb_comment_id=fb_id)


def lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def texts(path: Path, source: str = "fb_dom") -> list[str]:
    return [x["text"] for x in lines(path) if x["source"] == source]


def make(tmp_path: Path, clock=None, name="comments.jsonl") -> tuple[CommentLog, Path]:
    p = tmp_path / name
    return CommentLog(p, "live1", clock or FakeClock()), p


def seed(log: CommentLog, *items: Comment) -> None:
    for c in items:
        assert log.ingest_dom([c], rescan=False) == 1


def test_key_normalises_case_and_whitespace():
    assert C("  Ann ", "so  ld\n").key() == ("ann", "so ld")
    assert C("ANN", "sold").key() == C("ann", " sold ").key()


def test_live_repeats_kept(tmp_path):
    log, p = make(tmp_path)
    assert log.ingest_dom([C("ann", "sold")], rescan=False) == 1
    assert log.ingest_dom([C("ann", "sold")], rescan=False) == 1
    assert texts(p) == ["sold", "sold"]


def test_line_format_and_parent_dirs(tmp_path):
    clock = FakeClock(42.5)
    p = tmp_path / "deep" / "er" / "comments.jsonl"
    log = CommentLog(p, "live9", clock)
    log.ingest_dom(
        [Comment("ann", "hi", fb_comment_id="c1", created_at="2026-11-09T19:00:00Z")],
        rescan=False,
    )
    log.append_ssn(C("bob", "yo"))
    dom, ssn = lines(p)
    assert dom == {
        "captured_at": 42.5,
        "created_at": "2026-11-09T19:00:00Z",
        "source": "fb_dom",
        "fb_comment_id": "c1",
        "author": "ann",
        "text": "hi",
        "live_id": "live9",
    }
    assert ssn["source"] == "ssn"
    assert ssn["fb_comment_id"] is None and ssn["created_at"] is None


def test_every_line_is_fsynced(tmp_path, monkeypatch):
    import os

    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    log, _ = make(tmp_path)
    log.append_ssn(C("a", "x"))
    log.ingest_dom([C("a", "x")], rescan=False)
    assert len(calls) == 2


def test_rescan_skips_overlap(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "a"), C("u", "b"), C("u", "c"))
    n = log.ingest_dom([C("u", "b"), C("u", "c"), C("u", "d")], rescan=True)
    assert n == 1
    assert texts(p) == ["a", "b", "c", "d"]
    assert log.gaps == []


def test_rescan_keeps_true_repeat(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "a"), C("ann", "sold"))
    n = log.ingest_dom(
        [C("u", "a"), C("ann", "sold"), C("ann", "sold")], rescan=True
    )
    assert n == 1
    assert texts(p) == ["a", "sold", "sold"]
    assert log.gaps == []


def test_rescan_no_new(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "b"), C("u", "c"))
    assert log.ingest_dom([C("u", "b"), C("u", "c")], rescan=True) == 0
    assert texts(p) == ["b", "c"]
    assert log.gaps == []


def test_rescan_gap(tmp_path):
    clock = FakeClock(500.0)
    log, p = make(tmp_path, clock)
    seed(log, C("u", "x"), C("u", "y"))
    clock.advance(30)
    n = log.ingest_dom([C("u", "p"), C("u", "q")], rescan=True)
    assert n == 2
    assert log.gaps == [{"at": 530.0, "batch_size": 2}]
    ls = lines(p)
    gap_lines = [x for x in ls if x["source"] == "gap"]
    assert gap_lines == [
        {"source": "gap", "captured_at": 530.0, "batch_size": 2, "live_id": "live1"}
    ]
    assert texts(p) == ["x", "y", "p", "q"]


def test_rescan_empty_tail_no_gap(tmp_path):
    log, p = make(tmp_path)
    assert log.ingest_dom([C("u", "a"), C("u", "b")], rescan=True) == 2
    assert log.gaps == []
    assert [x["source"] for x in lines(p)] == ["fb_dom", "fb_dom"]


def test_rescan_empty_batch(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "a"))
    assert log.ingest_dom([], rescan=True) == 0
    assert log.gaps == []


def test_rescan_ambiguous_overlap_prefers_largest_i(tmp_path):
    # Log ends with a single "sold"; batch is [sold, sold]. Either visible
    # "sold" could be the logged one (equal k): the brief's rule picks the
    # largest i, i.e. the log's sold is the batch's last, so nothing is new.
    log, p = make(tmp_path)
    seed(log, C("u", "a"), C("ann", "sold"))
    assert log.ingest_dom([C("ann", "sold"), C("ann", "sold")], rescan=True) == 0
    assert texts(p) == ["a", "sold"]
    assert log.gaps == []


def test_rescan_gap_not_recorded_on_partial_overlap(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "a"), C("u", "b"), C("u", "c"))
    # only the last log entry (c) appears in the batch
    assert log.ingest_dom([C("u", "c"), C("u", "d"), C("u", "e")], rescan=True) == 2
    assert log.gaps == []
    assert texts(p) == ["a", "b", "c", "d", "e"]


def test_ids_dedupe(tmp_path):
    log, p = make(tmp_path)
    assert log.ingest_dom([C("ann", "sold", "id1")], rescan=False) == 1
    assert log.ingest_dom([C("ann", "sold", "id1")], rescan=False) == 0
    assert log.ingest_dom([C("ann", "sold", "id2")], rescan=False) == 1
    assert [x["fb_comment_id"] for x in lines(p)] == ["id1", "id2"]


def test_ids_dedupe_within_one_batch(tmp_path):
    log, p = make(tmp_path)
    n = log.ingest_dom([C("a", "x", "i1"), C("a", "x", "i1"), C("a", "x", "i2")], rescan=False)
    assert n == 2


def test_rescan_with_ids_skips_seen_ids_and_aligns(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "a", "1"), C("u", "b", "2"), C("u", "c", "3"))
    n = log.ingest_dom(
        [C("u", "b", "2"), C("u", "c", "3"), C("u", "d", "4")], rescan=True
    )
    assert n == 1
    assert [x["fb_comment_id"] for x in lines(p)] == ["1", "2", "3", "4"]
    assert log.gaps == []


def test_rescan_id_already_seen_is_skipped_even_in_appended_suffix(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "a", "1"), C("u", "b", "2"))
    # no overlap by key, but ID "2" was already seen -> skipped; gap recorded
    n = log.ingest_dom([C("u", "zzz", "2"), C("u", "new", "9")], rescan=True)
    assert n == 1
    assert texts(p) == ["a", "b", "new"]
    assert len(log.gaps) == 1


def test_rescan_mixed_ids_and_no_ids_align_on_all_keys(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "a"), C("ann", "sold", "s1"))
    n = log.ingest_dom(
        [C("u", "a"), C("ann", "sold", "s1"), C("ann", "sold", "s2")], rescan=True
    )
    assert n == 1
    assert [x["fb_comment_id"] for x in lines(p)][-1] == "s2"


def test_ssn_never_deduped(tmp_path):
    log, p = make(tmp_path)
    log.append_ssn(C("ann", "sold"))
    log.append_ssn(C("ann", "sold"))
    assert texts(p, "ssn") == ["sold", "sold"]


def test_ssn_not_part_of_dom_tail(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "a"))
    log.append_ssn(C("u", "b"))
    # rescan overlaps the dom log (a), ssn "b" must not influence alignment
    assert log.ingest_dom([C("u", "a"), C("u", "b")], rescan=True) == 1
    assert texts(p) == ["a", "b"]
    assert texts(p, "ssn") == ["b"]


def test_restart_continues_dedupe_by_key_tail(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "a"), C("ann", "sold"))
    log.append_ssn(C("ann", "sold"))
    log2 = CommentLog(p, "live1", FakeClock())
    n = log2.ingest_dom([C("u", "a"), C("ann", "sold"), C("ann", "sold")], rescan=True)
    assert n == 1
    assert texts(p) == ["a", "sold", "sold"]
    assert log2.gaps == []


def test_restart_continues_dedupe_by_id(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("ann", "sold", "id1"))
    log2 = CommentLog(p, "live1", FakeClock())
    assert log2.ingest_dom([C("ann", "sold", "id1")], rescan=False) == 0
    assert log2.ingest_dom([C("ann", "sold", "id2")], rescan=False) == 1


def test_restart_loads_gaps_and_ignores_gap_lines_in_tail(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "x"))
    log.ingest_dom([C("u", "p")], rescan=True)  # gap
    assert len(log.gaps) == 1
    log2 = CommentLog(p, "live1", FakeClock())
    assert log2.gaps == log.gaps
    assert log2.ingest_dom([C("u", "x"), C("u", "p")], rescan=True) == 0


def test_restart_tolerates_truncated_last_line(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "a"))
    with p.open("a") as f:
        f.write('{"source": "fb_dom", "author": "u", "te')  # crash mid-write
    log2 = CommentLog(p, "live1", FakeClock())
    assert log2.ingest_dom([C("u", "b")], rescan=False) == 1
    good = []
    for raw in p.read_text().splitlines():
        try:
            good.append(json.loads(raw))
        except json.JSONDecodeError:
            pass
    assert [x["text"] for x in good] == ["a", "b"]


def test_report(tmp_path):
    clock = FakeClock(1000.0)
    log, p = make(tmp_path, clock)
    # matched pair
    log.append_ssn(C("Ann", "sold"))
    clock.advance(5)
    log.ingest_dom([C("ann", "  sold ")], rescan=False)
    # only in ssn
    clock.advance(10)
    log.append_ssn(C("bob", "mine please"))
    # only in fb_dom
    clock.advance(10)
    log.ingest_dom([C("cat", "taking it")], rescan=False)
    # gap
    clock.advance(10)
    log.ingest_dom([C("zed", "p"), C("zed", "q")], rescan=True)

    rep = build_report(p)
    assert "ssn: 2" in rep
    assert "fb_dom: 4" in rep
    assert "matched: 1" in rep
    assert "only in ssn: 1" in rep
    assert "only in fb_dom: 3" in rep
    assert "gaps: 1" in rep
    assert "bob: mine please" in rep
    assert "cat: taking it" in rep
    # matched pair is not listed as unmatched
    assert "ann: sold" not in rep.lower()


def test_report_matching_is_one_to_one(tmp_path):
    clock = FakeClock(0.0)
    log, p = make(tmp_path, clock)
    log.append_ssn(C("ann", "sold"))
    clock.advance(1)
    log.append_ssn(C("ann", "sold"))
    clock.advance(1)
    log.ingest_dom([C("ann", "sold")], rescan=False)
    rep = build_report(p)
    assert "only in ssn: 1" in rep
    assert "only in fb_dom: 0" in rep
    assert "matched: 1" in rep


def test_report_window(tmp_path):
    clock = FakeClock(0.0)
    log, p = make(tmp_path, clock)
    log.append_ssn(C("ann", "sold"))
    clock.advance(300)
    log.ingest_dom([C("ann", "sold")], rescan=False)
    assert "only in ssn: 1" in build_report(p)
    assert "only in fb_dom: 1" in build_report(p)
    rep = build_report(p, window_s=600)
    assert "only in ssn: 0" in rep
    assert "only in fb_dom: 0" in rep


def test_report_stable_order_and_empty(tmp_path):
    clock = FakeClock(10.0)
    log, p = make(tmp_path, clock)
    log.append_ssn(C("b", "second"))
    clock.t = 5.0
    log.append_ssn(C("a", "first"))
    rep = build_report(p)
    assert rep.index("a: first") < rep.index("b: second")
    assert rep == build_report(p)

    empty = tmp_path / "none.jsonl"
    rep0 = build_report(empty)
    assert "only in ssn: 0" in rep0 and "gaps: 0" in rep0
