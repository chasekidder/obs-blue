import json
import logging
import re
import time
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


def test_rescan_unseen_id_not_dropped_by_key_alignment(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("ann", "sold", "id1"))
    n = log.ingest_dom([C("ann", "sold", "id1"), C("ann", "sold", "id2")], rescan=True)
    assert n == 1
    assert [x["fb_comment_id"] for x in lines(p)] == ["id1", "id2"]
    assert log.gaps == []


def test_rescan_unseen_id_before_aligned_run_is_appended(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("a", "a", "1"), C("b", "b", "2"))
    n = log.ingest_dom(
        [C("zz", "zz", "9"), C("a", "a", "1"), C("b", "b", "2")], rescan=True
    )
    assert n == 1
    assert [x["fb_comment_id"] for x in lines(p)] == ["1", "2", "9"]
    assert log.gaps == []


def test_rescan_id_items_append_in_batch_order_with_no_id_items(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "a"), C("u", "b"))
    # no-ID "a","b" are in the overlap (skipped); ID'd x is unseen -> appended;
    # no-ID "n" after the overlap is appended; order preserved.
    n = log.ingest_dom(
        [C("u", "a"), C("u", "b"), C("u", "x", "X"), C("u", "n")], rescan=True
    )
    assert n == 2
    assert texts(p) == ["a", "b", "x", "n"]


def test_rescan_no_id_item_in_overlap_skipped_while_id_item_beside_it_kept(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "a"), C("u", "b"))
    n = log.ingest_dom([C("u", "new", "N1"), C("u", "a"), C("u", "b")], rescan=True)
    assert n == 1
    assert texts(p) == ["a", "b", "new"]


def test_rescan_gap_still_appends_id_items_only_if_unseen(tmp_path):
    log, p = make(tmp_path)
    seed(log, C("u", "x", "1"), C("u", "y", "2"))
    n = log.ingest_dom([C("u", "p", "2"), C("u", "q", "3"), C("u", "r")], rescan=True)
    assert n == 2
    assert texts(p) == ["x", "y", "q", "r"]
    assert len(log.gaps) == 1


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


def test_loader_counts_and_warns_about_corrupt_lines(tmp_path, caplog):
    log, p = make(tmp_path)
    seed(log, C("u", "a"))
    with p.open("a") as f:
        f.write("garbage line\n")
        f.write("[1]\n")
    seed_log = CommentLog(p, "live1", FakeClock())
    assert seed_log.corrupt_lines == 2
    seed_log.ingest_dom([C("u", "b")], rescan=False)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="stream_pilot.comment_log"):
        log3 = CommentLog(p, "live1", FakeClock())
    assert log3.corrupt_lines == 2
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 2
    msgs = " ".join(r.getMessage() for r in warns)
    assert "line 2" in msgs and "line 3" in msgs
    # good records on both sides of the corruption still loaded
    assert log3.ingest_dom([C("u", "a"), C("u", "b")], rescan=True) == 0


def test_clean_file_has_zero_corrupt_lines(tmp_path):
    log, p = make(tmp_path)
    assert log.corrupt_lines == 0
    seed(log, C("u", "a"))
    assert CommentLog(p, "live1", FakeClock()).corrupt_lines == 0


def field(rep: str, name: str) -> int:
    """Value of a line-anchored `name: N` report header (exact match)."""
    m = re.findall(rf"^\s*{re.escape(name)}: (\d+)$", rep, re.M)
    assert len(m) == 1, f"{name!r} header found {len(m)} times in:\n{rep}"
    return int(m[0])


def has_entry(rep: str, who_what: str) -> bool:
    return any(
        ln.startswith("  ") and ln.endswith("  " + who_what) for ln in rep.split("\n")
    )


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
    assert field(rep, "ssn") == 2
    assert field(rep, "fb_dom") == 4
    assert field(rep, "matched") == 1
    assert field(rep, "only in ssn") == 1
    assert field(rep, "only in fb_dom") == 3
    assert field(rep, "gaps") == 1
    assert field(rep, "corrupt lines") == 0
    assert has_entry(rep, "bob: mine please")
    assert has_entry(rep, "cat: taking it")
    # matched pair is not listed as unmatched
    assert not has_entry(rep.lower(), "ann: sold")
    assert "ann: sold" not in rep.lower()


def test_report_field_helper_is_not_a_substring_match():
    rep = "  ssn: 20\nonly in ssn: 3\n"
    assert field(rep, "ssn") == 20
    assert field(rep, "only in ssn") == 3


def test_report_matching_is_one_to_one(tmp_path):
    clock = FakeClock(0.0)
    log, p = make(tmp_path, clock)
    log.append_ssn(C("ann", "sold"))
    clock.advance(1)
    log.append_ssn(C("ann", "sold"))
    clock.advance(1)
    log.ingest_dom([C("ann", "sold")], rescan=False)
    rep = build_report(p)
    assert field(rep, "only in ssn") == 1
    assert field(rep, "only in fb_dom") == 0
    assert field(rep, "matched") == 1


def test_report_window(tmp_path):
    clock = FakeClock(0.0)
    log, p = make(tmp_path, clock)
    log.append_ssn(C("ann", "sold"))
    clock.advance(300)
    log.ingest_dom([C("ann", "sold")], rescan=False)
    rep = build_report(p)
    assert field(rep, "only in ssn") == 1
    assert field(rep, "only in fb_dom") == 1
    rep = build_report(p, window_s=600)
    assert field(rep, "only in ssn") == 0
    assert field(rep, "only in fb_dom") == 0


def test_report_picks_closest_unmatched_dom(tmp_path):
    p = tmp_path / "c.jsonl"
    write_raw(
        p,
        [
            rec("ssn", 100.0, "ann", "sold"),
            rec("fb_dom", 20.0, "ann", "sold"),  # inside window, but farther
            rec("fb_dom", 99.0, "ann", "sold"),  # closest
        ],
    )
    rep = build_report(p, window_s=120)
    assert field(rep, "matched") == 1
    assert field(rep, "only in fb_dom") == 1
    assert "1970-01-01 00:00:20 UTC  ann: sold" in rep
    assert "1970-01-01 00:01:39 UTC" not in rep


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
    assert field(rep0, "only in ssn") == 0
    assert field(rep0, "gaps") == 0
    assert field(rep0, "corrupt lines") == 0


def rec(source: str, t: float, author: str, text: str) -> dict:
    return {
        "captured_at": t,
        "created_at": None,
        "source": source,
        "fb_comment_id": None,
        "author": author,
        "text": text,
        "live_id": "L",
    }


def write_raw(p: Path, recs: list[dict]) -> None:
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))


def test_report_survives_truncated_utf8_and_counts_corrupt(tmp_path):
    p = tmp_path / "c.jsonl"
    write_raw(p, [rec("ssn", 1.0, "ann", "sold")])
    with p.open("ab") as f:
        f.write(b'{"source": "ssn", "author": "x", "text": "\xe2\x82')  # cut mid-char
    rep = build_report(p)
    assert field(rep, "ssn") == 1
    assert field(rep, "corrupt lines") == 1


def test_report_counts_corrupt_lines_mid_file(tmp_path):
    p = tmp_path / "c.jsonl"
    with p.open("w") as f:
        f.write(json.dumps(rec("ssn", 1.0, "a", "x")) + "\n")
        f.write("not json at all\n")
        f.write("\n")  # blank lines are not corrupt
        f.write("[1, 2]\n")  # valid JSON but not a record
        f.write(json.dumps(rec("fb_dom", 2.0, "a", "x")) + "\n")
    rep = build_report(p)
    assert field(rep, "corrupt lines") == 2
    assert field(rep, "matched") == 1


def test_report_unicode_line_separators_in_text_do_not_split_records(tmp_path):
    log, p = make(tmp_path)
    log.append_ssn(C("ann", "a\u2028b\x0bc"))
    log.ingest_dom([C("ann", "a\u2028b\x0bc")], rescan=False)
    rep = build_report(p)
    assert field(rep, "corrupt lines") == 0
    assert field(rep, "matched") == 1


def test_report_scales_to_5000_by_5000(tmp_path):
    p = tmp_path / "big.jsonl"
    recs = []
    for i in range(5000):
        # a few hot keys (worst case for bucketing) plus many distinct ones
        who, what = ("ann", "sold") if i % 2 == 0 else (f"u{i}", f"t{i}")
        recs.append(rec("ssn", float(i), who, what))
        recs.append(rec("fb_dom", i + 0.5, who, what))
    recs.append(rec("fb_dom", 9999.0, "extra", "only dom"))
    write_raw(p, recs)
    t0 = time.perf_counter()
    rep = build_report(p)
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"build_report took {elapsed:.2f}s"
    assert field(rep, "ssn") == 5000
    assert field(rep, "fb_dom") == 5001
    assert field(rep, "matched") == 5000
    assert field(rep, "only in ssn") == 0
    assert field(rep, "only in fb_dom") == 1
