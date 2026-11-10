"""Per-stream comment record (comments.jsonl) with repeat-safe dedupe.

Comments are sales claims ("sold"), so a real repeat must never be merged.
Pure stdlib; no dependency on the rest of the package.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


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
        for raw in data.splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue  # truncated line from a crash mid-write
            if not isinstance(rec, dict):
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
        else:
            found = _new_suffix_start(tail, batch_keys)
            if found is None:
                start = 0
                gap = {"at": self._clock(), "batch_size": len(batch)}
                self.gaps.append(gap)
                self._write(
                    {
                        "source": "gap",
                        "captured_at": gap["at"],
                        "batch_size": gap["batch_size"],
                        "live_id": self.live_id,
                    }
                )
            else:
                start = found

        n = 0
        for c in batch[start:]:
            if self._is_seen(c):
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


def build_report(path: Path, window_s: float = 120.0) -> str:
    path = Path(path)
    ssn: list[dict] = []
    dom: list[dict] = []
    gaps = 0
    if path.exists():
        for raw in path.read_bytes().splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            src = rec.get("source")
            if src == "ssn":
                ssn.append(rec)
            elif src == "fb_dom":
                dom.append(rec)
            elif src == "gap":
                gaps += 1

    def ts(r: dict) -> float:
        return float(r.get("captured_at") or 0.0)

    def k(r: dict) -> tuple[str, str]:
        return _key(str(r.get("author", "")), str(r.get("text", "")))

    # Stable order: captured_at, ties keep file order (sort is stable).
    ssn.sort(key=ts)
    dom.sort(key=ts)

    # One-to-one matching: each ssn entry takes the closest unmatched fb_dom
    # entry with the same key within the window.
    matched_dom: set[int] = set()
    only_ssn: list[dict] = []
    matched = 0
    for s in ssn:
        best_j, best_d = None, None
        for j, d in enumerate(dom):
            if j in matched_dom or k(d) != k(s):
                continue
            dt = abs(ts(d) - ts(s))
            if dt <= window_s and (best_d is None or dt < best_d):
                best_j, best_d = j, dt
        if best_j is None:
            only_ssn.append(s)
        else:
            matched_dom.add(best_j)
            matched += 1
    only_dom = [d for j, d in enumerate(dom) if j not in matched_dom]

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
        "",
        f"== only in ssn ({len(only_ssn)}) ==",
        *[entry(r) for r in only_ssn],
        "",
        f"== only in fb_dom ({len(only_dom)}) ==",
        *[entry(r) for r in only_dom],
        "",
    ]
    return "\n".join(out)
