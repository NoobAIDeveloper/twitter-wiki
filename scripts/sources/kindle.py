#!/usr/bin/env python3
"""
Kindle clippings source adapter.

Parses the idiosyncratic `My Clippings.txt` file that Kindle devices write
to their root when plugged in via USB. Emits one Item per highlight.

Format (each entry is 4+ lines separated by `==========`):

    Book Title (Author Name)
    - Your Highlight on Location 1234-1256 | Added on Monday, April 5, 2024 9:42:01 PM

    Highlight text spanning one or more lines.
    ==========

Notes and bookmarks follow the same shape but with different metadata lines;
we keep highlights and notes, skip pure bookmark entries (no text body).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import Item


SOURCE_ID = "kindle"
SEPARATOR = "=========="

_META_RE = re.compile(
    r"^-\s*(?:Your\s+)?(?P<kind>Highlight|Note|Bookmark)\b.*?"
    r"(?:Added\s+on\s+(?P<date>.+))?$",
    re.IGNORECASE,
)

_DATE_FORMATS = (
    "%A, %B %d, %Y %I:%M:%S %p",
    "%A, %d %B %Y %H:%M:%S",
    "%A, %B %d, %Y",
)


def _parse_date(s: str | None) -> str:
    if not s:
        return ""
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return s  # return raw if we can't parse


def _parse_entry(block: str) -> dict[str, Any] | None:
    """Parse one clipping entry into a dict, or None if unparseable/bookmark."""
    lines = [ln.rstrip() for ln in block.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None

    title_line = lines[0].lstrip("\ufeff").strip()  # strip BOM on first entry
    meta_line = lines[1]
    body = "\n".join(lines[2:]).strip()

    # Title + author: "Title (Author Name)" — split on the last paren.
    title = title_line
    author = None
    if title_line.endswith(")") and "(" in title_line:
        idx = title_line.rfind("(")
        title = title_line[:idx].strip()
        author = title_line[idx + 1 : -1].strip() or None

    m = _META_RE.search(meta_line)
    kind = "Highlight"
    date_str = None
    if m:
        kind = m.group("kind").title()
        date_str = m.group("date")

    if kind.lower() == "bookmark":
        return None  # pure location marker, no content
    if not body:
        return None

    return {
        "title": title,
        "author": author,
        "kind": kind,
        "timestamp": _parse_date(date_str),
        "body": body,
    }


def parse_clippings(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    entries: list[dict[str, Any]] = []
    for block in raw.split(SEPARATOR):
        if not block.strip():
            continue
        entry = _parse_entry(block)
        if entry:
            entries.append(entry)
    return entries


def sync(kb_dir: Path | None, *, clippings_path: Path) -> list[dict[str, Any]]:
    """Import highlights from a `My Clippings.txt` file."""
    path = clippings_path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Kindle clippings file not found: {path}")

    entries = parse_clippings(path)
    items: list[Item] = []
    # Dedupe by (title, body) hash so re-importing the same file doesn't
    # create duplicate Items.
    seen: set[tuple[str, str]] = set()
    for i, entry in enumerate(entries):
        key = (entry["title"], entry["body"][:200])
        if key in seen:
            continue
        seen.add(key)

        text = entry["body"]
        if entry["author"]:
            header = f"{entry['title']} — {entry['author']}"
        else:
            header = entry["title"]
        text = f"{header}\n\n{text}"

        items.append(
            Item(
                id=f"{SOURCE_ID}:{abs(hash(key))}",
                source=SOURCE_ID,
                text=text,
                timestamp=entry["timestamp"],
                author=entry["author"],
                url=None,
                engagement=None,
                media=[],
                metadata={
                    "book_title": entry["title"],
                    "kind": entry["kind"],
                },
            )
        )
    return [it.to_json() for it in items]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Dump Kindle clippings as Items.")
    ap.add_argument("clippings", type=Path, help="Path to My Clippings.txt")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    items = sync(None, clippings_path=args.clippings)
    print(f"[kindle] {len(items)} highlight(s)")
    for it in items[: args.limit]:
        print(f"  {it['metadata']['book_title'][:60]} · {it['text'][:80]!r}")
