#!/usr/bin/env python3
"""
Apply the cluster map to the normalized item corpus and emit per-topic batches.

Reads `<kb>/.twitter-wiki/cluster-map.json` and `<kb>/raw/items.jsonl` (falling
back to the legacy `<kb>/raw/bookmarks.jsonl` for older KBs), matches each
item against every topic's rules (multi-assign — a single item can land in
more than one batch), and writes:

    <kb>/raw/bookmarks/<topic>.md      one markdown file per topic
    <kb>/raw/bookmarks/_unsorted.md    items that matched no topic
    <kb>/raw/bookmarks/_manifest.md    index with counts

Claude generates cluster-map.json on first ingest by sampling the user's
actual items. This script is the deterministic applier — it never invents
topics, it just routes.

Usage:
    python3 scripts/preprocess.py --kb <kb-path>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---- cluster map ------------------------------------------------------------

@dataclass
class Topic:
    name: str
    description: str
    keywords: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    regexes: list[re.Pattern[str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # empty = all sources

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Topic":
        name = str(raw["name"]).strip()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise ValueError(
                f"topic name {name!r} must be kebab-case (lowercase, hyphens only)"
            )
        match = raw.get("match") or {}
        patterns = []
        for pat in match.get("regex", []) or []:
            try:
                patterns.append(re.compile(pat, re.IGNORECASE))
            except re.error as e:
                raise ValueError(f"topic {name!r}: invalid regex {pat!r}: {e}")
        return cls(
            name=name,
            description=str(raw.get("description", "")).strip(),
            keywords=[k.lower() for k in (match.get("keywords") or [])],
            hashtags=[h.lstrip("#").lower() for h in (match.get("hashtags") or [])],
            authors=[a.lstrip("@").lower() for a in (match.get("authors") or [])],
            regexes=patterns,
            sources=[s.lower() for s in (match.get("sources") or [])],
        )

    def matches(self, item: dict[str, Any]) -> bool:
        # Accept both normalized Item dicts (fields: text/author/source) and
        # legacy X bookmark dicts (fields: text/authorHandle) so preprocess
        # still works on KBs that haven't been re-synced after the refactor.
        source = (item.get("source") or "x").lower()
        if self.sources and source not in self.sources:
            return False

        text = (item.get("text") or "").lower()
        author = (item.get("author") or item.get("authorHandle") or "")
        author = author.lstrip("@").lower()

        has_positive_rule = bool(
            self.authors or self.keywords or self.hashtags or self.regexes
        )
        # Source-only topics (no other rules) treat the source match itself
        # as sufficient — i.e. "all items from this source go here."
        if self.sources and not has_positive_rule:
            return True

        if self.authors and author in self.authors:
            return True
        if self.keywords and any(kw in text for kw in self.keywords):
            return True
        if self.hashtags:
            tags = {m.group(1).lower() for m in re.finditer(r"#(\w+)", text)}
            if tags & set(self.hashtags):
                return True
        if self.regexes and any(p.search(text) for p in self.regexes):
            return True
        return False


def load_cluster_map(path: Path) -> list[Topic]:
    if not path.exists():
        sys.exit(
            f"error: {path} not found.\n"
            "Bootstrap it first by sampling bookmarks and writing the topic map."
        )
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"error: {path} is not valid JSON: {e}")
    topics_raw = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(topics_raw, list) or not topics_raw:
        sys.exit(f"error: {path} must define a non-empty `topics` list")
    topics = [Topic.from_dict(t) for t in topics_raw]
    names = [t.name for t in topics]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        sys.exit(f"error: duplicate topic names in cluster-map.json: {sorted(dupes)}")
    return topics


# ---- bookmark rendering -----------------------------------------------------

def _fmt_count(n: int | None) -> str:
    if not n:
        return "0"
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def _render_x(item: dict[str, Any]) -> str:
    # Supports both normalized Items and legacy bookmark dicts.
    meta = item.get("metadata") or {}
    handle = item.get("author") or item.get("authorHandle") or "unknown"
    name = meta.get("authorName") or item.get("authorName") or handle
    posted = item.get("timestamp") or item.get("postedAt") or ""
    url = item.get("url") or ""
    text = (item.get("text") or "").strip()

    eng = item.get("engagement") or {}
    likes = _fmt_count(eng.get("likeCount"))
    reposts = _fmt_count(eng.get("repostCount"))
    replies = _fmt_count(eng.get("replyCount"))

    lines = [
        f"### @{handle} — {name}",
        f"- posted: {posted}",
        f"- url: {url}",
        f"- engagement: {likes} likes · {reposts} reposts · {replies} replies",
    ]

    media = item.get("media") or []
    if media:
        lines.append(f"- media: {len(media)} item(s)")
    links = meta.get("links") or item.get("links") or []
    if links:
        lines.append("- links: " + ", ".join(links[:5]))

    quoted = meta.get("quotedTweet") or item.get("quotedTweet")
    if quoted:
        q_handle = quoted.get("authorHandle") or "unknown"
        q_text = (quoted.get("text") or "").strip().replace("\n", " ")
        if len(q_text) > 240:
            q_text = q_text[:237] + "..."
        lines.append(f"- quotes @{q_handle}: {q_text}")

    lines.append("")
    if text:
        for line in text.splitlines():
            lines.append(f"> {line}" if line else ">")

    return "\n".join(lines).rstrip() + "\n"


def _render_generic(item: dict[str, Any]) -> str:
    source = item.get("source") or "unknown"
    author = item.get("author")
    header = f"### [{source}]"
    if author:
        header += f" @{author}"
    ts = item.get("timestamp") or ""
    url = item.get("url") or ""

    lines = [header]
    if ts:
        lines.append(f"- at: {ts}")
    if url:
        lines.append(f"- url: {url}")
    meta = item.get("metadata") or {}
    for k in ("conversation_id", "book_title", "repo", "folder"):
        if k in meta:
            lines.append(f"- {k}: {meta[k]}")

    text = (item.get("text") or "").strip()
    lines.append("")
    if text:
        for line in text.splitlines():
            lines.append(f"> {line}" if line else ">")

    return "\n".join(lines).rstrip() + "\n"


def render_item(item: dict[str, Any]) -> str:
    source = (item.get("source") or "x").lower()
    if source == "x":
        return _render_x(item)
    return _render_generic(item)


# Legacy alias — preserved for any external callers.
def render_bookmark(bm: dict[str, Any]) -> str:
    return _render_x(bm)


def write_batch(
    path: Path, topic: Topic | None, items: list[dict[str, Any]]
) -> None:
    if topic is not None:
        header = [f"# {topic.name}", ""]
        if topic.description:
            header += [topic.description, ""]
    else:
        header = [
            "# _unsorted",
            "",
            "Items that matched no topic in cluster-map.json. Add a rule "
            "(or a new topic) and re-run preprocess to route them.",
            "",
        ]
    # Show source breakdown so Claude can see at a glance what's in each batch.
    source_counts: dict[str, int] = {}
    for it in items:
        s = (it.get("source") or "x").lower()
        source_counts[s] = source_counts.get(s, 0) + 1
    source_summary = ", ".join(
        f"{s}: {c}" for s, c in sorted(source_counts.items())
    ) or "empty"
    header += [
        f"Generated by preprocess.py · {len(items)} item(s) ({source_summary}).",
        "Do not hand-edit — regenerated on every preprocess run.",
        "",
        "---",
        "",
    ]
    body = "\n".join(render_item(it) for it in items)
    path.write_text("\n".join(header) + body)


def write_manifest(
    path: Path,
    topics: list[Topic],
    counts: dict[str, int],
    unsorted_count: int,
    total: int,
    generated_at: str,
    source_counts: dict[str, int] | None = None,
) -> None:
    lines = [
        "# Item batch manifest",
        "",
        f"Generated: {generated_at}",
        f"Total items: {total}",
    ]
    if source_counts:
        summary = ", ".join(
            f"{s}: {c}" for s, c in sorted(source_counts.items())
        )
        lines.append(f"Sources: {summary}")
    lines += [
        "",
        "| Topic | Items | Description |",
        "|---|---:|---|",
    ]
    for t in topics:
        lines.append(f"| [{t.name}]({t.name}.md) | {counts.get(t.name, 0)} | {t.description} |")
    lines.append(f"| [_unsorted](_unsorted.md) | {unsorted_count} | No match |")
    path.write_text("\n".join(lines) + "\n")


# ---- main -------------------------------------------------------------------

def load_items_or_bookmarks(kb: Path) -> list[dict[str, Any]]:
    """Prefer raw/items.jsonl (normalized, multi-source). Fall back to
    raw/bookmarks.jsonl for KBs synced before the Item refactor — those
    legacy dicts still work because matches() and render_item() accept
    either shape."""
    items_path = kb / "raw" / "items.jsonl"
    legacy_path = kb / "raw" / "bookmarks.jsonl"

    def _read(path: Path, label: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i, line in enumerate(path.read_text().split("\n"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                sys.exit(f"error: {label} line {i}: {e}")
        return out

    if items_path.exists():
        return _read(items_path, "items.jsonl")
    if legacy_path.exists():
        print(
            "note: reading legacy raw/bookmarks.jsonl. Re-run /kb-sync "
            "to regenerate raw/items.jsonl in the new format.",
            file=sys.stderr,
        )
        return _read(legacy_path, "bookmarks.jsonl")
    sys.exit(f"error: neither {items_path} nor {legacy_path} found. Run sync.py first.")


# Kept for backward compatibility with external imports.
def load_bookmarks(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        sys.exit(f"error: {path} not found. Run sync.py first.")
    out = []
    for i, line in enumerate(path.read_text().split("\n"), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            sys.exit(f"error: {path.name} line {i}: {e}")
    return out


def clean_batch_dir(batch_dir: Path) -> None:
    # Only remove files we own: *.md at the top level. Leave subdirs alone.
    if not batch_dir.exists():
        batch_dir.mkdir(parents=True)
        return
    for child in batch_dir.iterdir():
        if child.is_file() and child.suffix == ".md":
            child.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply cluster-map.json to bookmarks.")
    ap.add_argument("--kb", required=True, type=Path, help="KB root directory")
    args = ap.parse_args()

    kb: Path = args.kb.resolve()
    map_path = kb / ".twitter-wiki" / "cluster-map.json"
    batch_dir = kb / "raw" / "bookmarks"

    topics = load_cluster_map(map_path)
    items = load_items_or_bookmarks(kb)

    buckets: dict[str, list[dict[str, Any]]] = {t.name: [] for t in topics}
    unsorted: list[dict[str, Any]] = []

    for it in items:
        hit = False
        for t in topics:
            if t.matches(it):
                buckets[t.name].append(it)
                hit = True
        if not hit:
            unsorted.append(it)

    clean_batch_dir(batch_dir)
    for t in topics:
        write_batch(batch_dir / f"{t.name}.md", t, buckets[t.name])
    write_batch(batch_dir / "_unsorted.md", None, unsorted)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    counts = {name: len(bms) for name, bms in buckets.items()}
    source_counts: dict[str, int] = {}
    for it in items:
        s = (it.get("source") or "x").lower()
        source_counts[s] = source_counts.get(s, 0) + 1
    write_manifest(
        batch_dir / "_manifest.md",
        topics,
        counts,
        unsorted_count=len(unsorted),
        total=len(items),
        generated_at=generated_at,
        source_counts=source_counts,
    )

    print(f"preprocessed {len(items)} item(s) into {len(topics)} topic(s)")
    for t in topics:
        print(f"  {t.name}: {counts[t.name]}")
    print(f"  _unsorted: {len(unsorted)}")
    if source_counts:
        src_summary = ", ".join(f"{s}: {c}" for s, c in sorted(source_counts.items()))
        print(f"  sources: {src_summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
