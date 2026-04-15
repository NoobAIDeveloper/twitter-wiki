#!/usr/bin/env python3
"""
Common Item schema and helpers shared by every source adapter.

Each source module (x, claude_code, chatgpt, ...) normalizes its raw data
into a list of Items. The KB stores all Items from all sources in a single
file, `raw/items.jsonl`, which preprocess.py consumes — so preprocess never
has to know whether a given record came from a tweet or a chat turn.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Item:
    """Normalized unit that flows through preprocess and synthesis.

    Fields:
        id: Source-prefixed unique id, e.g. "x:1930244636578119863" or
            "chatgpt:conv_abc/turn_3". Must be stable across syncs so we
            can dedupe.
        source: Short source identifier. One of "x", "claude-code",
            "chatgpt", "claude-ai", "kindle", "github", "chrome", ...
        author: Handle/username of the content's author, or None for
            content the user produced themselves (e.g. their own chat
            turns).
        text: Primary textual content. For chats, this is the Q+A pair
            concatenated with role labels.
        url: Canonical link back to the source, if one exists.
        timestamp: ISO 8601 string (UTC preferred).
        engagement: Optional source-specific dict (e.g. {"likeCount": 12}).
        media: Optional list of media URLs / descriptors.
        metadata: Arbitrary source-specific extras (conversation_id,
            book title, repo stars, etc.).
    """

    id: str
    source: str
    text: str
    timestamp: str
    author: str | None = None
    url: str | None = None
    engagement: dict[str, Any] | None = None
    media: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def load_items(path: Path) -> list[dict[str, Any]]:
    """Load items.jsonl. Returns raw dicts (not Item) so downstream code can
    treat source-specific fields loosely. Split on "\n" to tolerate Unicode
    line separators inside text fields."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"items.jsonl line {i}: {exc}") from exc
    return out


def write_items(path: Path, items: list[dict[str, Any]]) -> None:
    """Atomically rewrite items.jsonl."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = "\n".join(json.dumps(it, ensure_ascii=False) for it in items)
    if body:
        body += "\n"
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def merge_items(
    existing: list[dict[str, Any]],
    new: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Dedupe by id; newer record wins. Returns (merged, added_count)."""
    by_id: dict[str, dict[str, Any]] = {}
    for rec in existing:
        rid = rec.get("id")
        if rid:
            by_id[rid] = rec
    added = 0
    for rec in new:
        rid = rec.get("id")
        if not rid:
            continue
        if rid not in by_id:
            added += 1
        by_id[rid] = rec
    merged = sorted(
        by_id.values(),
        key=lambda r: r.get("timestamp") or "",
        reverse=True,
    )
    return merged, added


def replace_source_items(
    kb_dir: Path,
    source_id: str,
    new_items: list[dict[str, Any]],
) -> tuple[int, int]:
    """Rewrite raw/items.jsonl, replacing all items with the given source.

    Other sources' items are preserved untouched. Returns (total_items,
    items_in_this_source).
    """
    items_path = kb_dir / "raw" / "items.jsonl"
    try:
        existing = load_items(items_path)
    except ValueError:
        existing = []
    kept = [it for it in existing if it.get("source") != source_id]
    combined, _ = merge_items(kept, new_items)
    write_items(items_path, combined)
    return len(combined), len(new_items)
