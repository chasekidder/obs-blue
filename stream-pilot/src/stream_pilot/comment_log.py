"""Per-stream comment record (comments.jsonl) with repeat-safe dedupe.

Comments are sales claims ("sold"), so a real repeat must never be merged.
Pure stdlib; no dependency on the rest of the package.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Comment:
    author: str
    text: str
    fb_comment_id: str | None = None
    created_at: str | None = None

    def key(self) -> tuple[str, str]:
        return (self.author.strip().casefold(), " ".join(self.text.split()))


def _key(author: str, text: str) -> tuple[str, str]:
    return Comment(author or "", text or "").key()


def _new_suffix_start(tail: list[tuple], batch: list[tuple]) -> int | None:
    """Index in `batch` where genuinely new items start, or None if the
    batch does not overlap the end of `tail` at all."""
    best_k, best_i = 0, None
    for i in range(len(batch), 0, -1):
        k = 0
        while k < i and k < len(tail) and tail[-1 - k] == batch[i - 1 - k]:
            k += 1
        if k > best_k:
            best_k, best_i = k, i
    return best_i  # None => no overlap


class CommentLog:
    def __init__(
        self,
        path: Path,
        live_id: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.live_id = live_id
        self._clock = clock
        self.gaps: list[dict] = []
        self.corrupt_lines = 0
        self._dom_keys: list[tuple[str, str]] = []
        self._seen_ids: set[str] = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    # -- persistence -----------------------------------------------------

    def _load_existing(self) -> None:
        """Resume after a restart: rebuild the fb_dom tail, seen IDs and gaps."""
        if not self.path.exists():
            return
        with self.path.open("rb") as f:
            data = f.read()
        for lineno, raw in enumerate(data.splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                rec = None
            if not isinstance(rec, dict):
                self.corrupt_lines += 1
                log.warning(
                    "%s: skipping unparseable line %d", self.path, lineno
                )
                continue
            src = rec.get("source")
            if src == "fb_dom":
                self._dom_keys.append(
                    _key(str(rec.get("author", "")), str(rec.get("text", "")))
                )
                if rec.get("fb_comment_id"):
                    self._seen_ids.add(rec["fb_comment_id"])
            elif src == "gap":
                self.gaps.append(
                    {
                        "at": rec.get("captured_at"),
                        "batch_size": rec.get("batch_size"),
                    }
                )
        if data and not data.endswith(b"\n"):
            # Do not glue the next record onto a truncated one.
            self._write_raw("\n")

    def _write_raw(self, s: str) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(s)
            f.flush()
            os.fsync(f.fileno())

    def _write(self, rec: dict) -> None:
        self._write_raw(json.dumps(rec, ensure_ascii=False) + "\n")

    def _write_comment(self, source: str, c: Comment) -> None:
        self._write(
            {
                "captured_at": self._clock(),
                "created_at": c.created_at,
                "source": source,
                "fb_comment_id": c.fb_comment_id,
                "author": c.author,
                "text": c.text,
                "live_id": self.live_id,
            }
        )

    # -- capture ---------------------------------------------------------

    def append_ssn(self, c: Comment) -> None:
        """SSN lines are never deduped (neither within SSN nor against fb_dom)."""
        self._write_comment("ssn", c)

    def _append_dom(self, c: Comment) -> None:
        self._write_comment("fb_dom", c)
        self._dom_keys.append(c.key())
        if c.fb_comment_id:
            self._seen_ids.add(c.fb_comment_id)

    def _is_seen(self, c: Comment) -> bool:
        return bool(c.fb_comment_id) and c.fb_comment_id in self._seen_ids

    def _record_gap(self, batch_size: int) -> None:
        gap = {"at": self._clock(), "batch_size": batch_size}
        self.gaps.append(gap)
        self._write(
            {
                "source": "gap",
                "captured_at": gap["at"],
                "batch_size": gap["batch_size"],
                "live_id": self.live_id,
            }
        )

    def ingest_dom(self, batch: list[Comment], *, rescan: bool) -> int:
        """Record fb_dom comments; returns the number of lines appended."""
        if not batch:
            return 0

        if not rescan:
            # Live mutations: by ID if present, otherwise always append.
            n = 0
            for c in batch:
                if self._is_seen(c):
                    continue
                self._append_dom(c)
                n += 1
            return n

        # Rescan of the visible history: align in order against the log's end.
        batch_keys = [c.key() for c in batch]
        tail = self._dom_keys[-len(batch):]
        if not tail:
            start = 0
        elif all(c.fb_comment_id for c in batch):
            # Every item is deduped by its ID below; the history overlaps the log
            # iff one of its IDs was seen (texts may differ, e.g. after an update).
            start = 0
            if not any(self._is_seen(c) for c in batch):
                self._record_gap(len(batch))
        else:
            found = _new_suffix_start(tail, batch_keys)
            if found is None:
                start = 0
                self._record_gap(len(batch))
            else:
                start = found

        # ID'd items: appended iff their ID is unseen, wherever alignment
        # landed (dedupe by ID only). No-ID items: only those from `start` on.
        n = 0
        for idx, c in enumerate(batch):
            if c.fb_comment_id:
                if self._is_seen(c):
                    continue
            elif idx < start:
                continue
            self._append_dom(c)
            n += 1
        return n


# -- report --------------------------------------------------------------


def _fmt_ts(t: float) -> str:
    try:
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (OverflowError, OSError, ValueError):
        return f"t={t}"


# SSN relays a reply as "Replying to <name>: <text>"; the DOM capture has just the text.
_SSN_REPLY_PREFIX_RE = re.compile(r"^\s*Replying to [^:]+:\s*")


def build_report(path: Path, window_s: float = 120.0) -> str:
    path = Path(path)
    ssn: list[dict] = []
    dom: list[dict] = []
    gaps = 0
    corrupt = 0
    if path.exists():
        text = path.read_bytes().decode("utf-8", errors="replace")
        # split on "\n" only: str.splitlines() also splits on U+2028 etc.,
        # which json.dumps(ensure_ascii=False) can emit inside a text field.
        for raw in text.split("\n"):
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                rec = None
            if not isinstance(rec, dict):
                corrupt += 1
                continue
            src = rec.get("source")
            if src == "ssn":
                ssn.append(rec)
            elif src == "fb_dom":
                dom.append(rec)
            elif src == "gap":
                gaps += 1

    def ts(r: dict) -> float:
        try:
            return float(r.get("captured_at") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def k(r: dict) -> tuple[str, str]:
        text = str(r.get("text", ""))
        if r.get("source") == "ssn":
            text = _SSN_REPLY_PREFIX_RE.sub("", text)
        return _key(str(r.get("author", "")), text)

    # Stable order: captured_at, ties keep file order (sort is stable).
    ssn.sort(key=ts)
    dom.sort(key=ts)

    # Bucket fb_dom by key once. Each bucket keeps parallel lists (sorted by
    # time) of timestamps and indices into `dom`; matched entries are deleted.
    buckets: dict[tuple[str, str], tuple[list[float], list[int]]] = {}
    for j, d in enumerate(dom):
        times, idxs = buckets.setdefault(k(d), ([], []))
        times.append(ts(d))
        idxs.append(j)

    # One-to-one matching: each ssn entry (in time order) takes the closest
    # still-unmatched fb_dom entry with the same key within the window
    # (the earlier one on a tie).
    only_ssn: list[dict] = []
    matched = 0
    for s in ssn:
        bucket = buckets.get(k(s))
        if bucket is None or not bucket[0]:
            only_ssn.append(s)
            continue
        times, idxs = bucket
        t = ts(s)
        pos = bisect_left(times, t)
        best = None
        if pos > 0:
            best = pos - 1
        if pos < len(times) and (
            best is None or times[pos] - t < t - times[best]
        ):
            best = pos
        if best is not None and abs(times[best] - t) <= window_s:
            del times[best]
            del idxs[best]
            matched += 1
        else:
            only_ssn.append(s)
    remaining = sorted(j for _, idxs in buckets.values() for j in idxs)
    only_dom = [dom[j] for j in remaining]

    def entry(r: dict) -> str:
        return f"  {_fmt_ts(ts(r))}  {r.get('author', '')}: {r.get('text', '')}"

    out = [
        "stream-pilot comment report",
        f"window_s: {window_s:g}",
        "comments by source:",
        f"  fb_dom: {len(dom)}",
        f"  ssn: {len(ssn)}",
        f"matched: {matched}",
        f"only in ssn: {len(only_ssn)}",
        f"only in fb_dom: {len(only_dom)}",
        f"gaps: {gaps}",
        f"corrupt lines: {corrupt}",
        "",
        f"== only in ssn ({len(only_ssn)}) ==",
        *[entry(r) for r in only_ssn],
        "",
        f"== only in fb_dom ({len(only_dom)}) ==",
        *[entry(r) for r in only_dom],
        "",
    ]
    return "\n".join(out)
