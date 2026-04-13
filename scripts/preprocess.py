#!/usr/bin/env python3
"""
Apply the cluster map to bookmarks and emit per-topic batch files.

Reads `<kb>/.twitter-wiki/cluster-map.json` and `<kb>/raw/bookmarks.jsonl`,
matches each bookmark against every topic's rules (multi-assign — a single
bookmark can land in more than one batch), and writes:

    <kb>/raw/bookmarks/<topic>.md      one markdown file per topic
    <kb>/raw/bookmarks/_unsorted.md    bookmarks that matched no topic
    <kb>/raw/bookmarks/_manifest.md    index with counts

Claude generates cluster-map.json on first ingest by sampling the user's
actual bookmarks. This script is the deterministic applier — it never
invents topics, it just routes.

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
        )

    def matches(self, bm: dict[str, Any]) -> bool:
        text = (bm.get("text") or "").lower()
        handle = (bm.get("authorHandle") or "").lstrip("@").lower()

        if self.authors and handle in self.authors:
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


def render_bookmark(bm: dict[str, Any]) -> str:
    handle = bm.get("authorHandle") or "unknown"
    name = bm.get("authorName") or handle
    posted = bm.get("postedAt") or ""
    url = bm.get("url") or ""
    text = (bm.get("text") or "").strip()

    eng = bm.get("engagement") or {}
    likes = _fmt_count(eng.get("likeCount"))
    reposts = _fmt_count(eng.get("repostCount"))
    replies = _fmt_count(eng.get("replyCount"))

    lines = [
        f"### @{handle} — {name}",
        f"- posted: {posted}",
        f"- url: {url}",
        f"- engagement: {likes} likes · {reposts} reposts · {replies} replies",
    ]

    media = bm.get("media") or []
    if media:
        lines.append(f"- media: {len(media)} item(s)")
    links = bm.get("links") or []
    if links:
        lines.append("- links: " + ", ".join(links[:5]))

    quoted = bm.get("quotedTweet")
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


def write_batch(
    path: Path, topic: Topic | None, bookmarks: list[dict[str, Any]]
) -> None:
    if topic is not None:
        header = [f"# {topic.name}", ""]
        if topic.description:
            header += [topic.description, ""]
    else:
        header = [
            "# _unsorted",
            "",
            "Bookmarks that matched no topic in cluster-map.json. Add a rule "
            "(or a new topic) and re-run preprocess to route them.",
            "",
        ]
    header += [
        f"Generated by preprocess.py · {len(bookmarks)} bookmark(s).",
        "Do not hand-edit — regenerated on every preprocess run.",
        "",
        "---",
        "",
    ]
    body = "\n".join(render_bookmark(bm) for bm in bookmarks)
    path.write_text("\n".join(header) + body)


def write_manifest(
    path: Path,
    topics: list[Topic],
    counts: dict[str, int],
    unsorted_count: int,
    total: int,
    generated_at: str,
) -> None:
    lines = [
        "# Bookmark batch manifest",
        "",
        f"Generated: {generated_at}",
        f"Total bookmarks: {total}",
        "",
        "| Topic | Bookmarks | Description |",
        "|---|---:|---|",
    ]
    for t in topics:
        lines.append(f"| [{t.name}]({t.name}.md) | {counts.get(t.name, 0)} | {t.description} |")
    lines.append(f"| [_unsorted](_unsorted.md) | {unsorted_count} | No match |")
    path.write_text("\n".join(lines) + "\n")


# ---- main -------------------------------------------------------------------

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
            sys.exit(f"error: bookmarks.jsonl line {i}: {e}")
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
    jsonl_path = kb / "raw" / "bookmarks.jsonl"
    batch_dir = kb / "raw" / "bookmarks"

    topics = load_cluster_map(map_path)
    bookmarks = load_bookmarks(jsonl_path)

    buckets: dict[str, list[dict[str, Any]]] = {t.name: [] for t in topics}
    unsorted: list[dict[str, Any]] = []

    for bm in bookmarks:
        hit = False
        for t in topics:
            if t.matches(bm):
                buckets[t.name].append(bm)
                hit = True
        if not hit:
            unsorted.append(bm)

    clean_batch_dir(batch_dir)
    for t in topics:
        write_batch(batch_dir / f"{t.name}.md", t, buckets[t.name])
    write_batch(batch_dir / "_unsorted.md", None, unsorted)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    counts = {name: len(bms) for name, bms in buckets.items()}
    write_manifest(
        batch_dir / "_manifest.md",
        topics,
        counts,
        unsorted_count=len(unsorted),
        total=len(bookmarks),
        generated_at=generated_at,
    )

    print(f"preprocessed {len(bookmarks)} bookmarks into {len(topics)} topic(s)")
    for t in topics:
        print(f"  {t.name}: {counts[t.name]}")
    print(f"  _unsorted: {len(unsorted)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
