#!/usr/bin/env python3
"""
Granola source adapter.

Granola is a macOS-only meeting notetaker. Its desktop app stores every
meeting in a single local JSON cache at
`~/Library/Application Support/Granola/cache-v3.json`. No cloud API
involved — this adapter reads the file directly, same pattern as the
browser-bookmarks and claude-code adapters.

The cache is wrapped as `{"cache": "<json-string>"}` (pre-v6,
double-encoded) or `{"cache": {...}}` (v6+, direct). Either way, the
inner object has a `state` key with:

- `documents`: map of doc_id → meeting record (title, notes, dates, …)
- `meetingsMetadata`: map of doc_id → extra metadata (attendees, calendar)
- `transcripts`: map of doc_id → list of transcript segments
- `documentPanels`: map of doc_id → { panel_id → { original_content: html,
   content: prosemirror } } — contains the AI-generated summary
- `documentLists` / `documentListsMetadata`: folder structure

One meeting becomes N Items (one per AI-summary H1/H2 section, or one
per notes heading if no summary, or size-chunked transcript if neither).
Item id: `granola:<meeting_id>:<chunk_index>`.

Config (optional) — override the cache path in `.engram/sources.json`:

    {"granola": {"cache_path": "/custom/path/cache-v3.json"}}
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .base import (
    Item,
    chunk_by_headings,
    chunk_by_size,
    drop_items_by_id_prefix,
    load_items,
    make_chunk_items,
)


SOURCE_ID = "granola"
DEFAULT_CACHE_PATH = Path.home() / "Library" / "Application Support" / "Granola" / "cache-v3.json"


# ---- config / state --------------------------------------------------------

def _load_cache_path(kb_dir: Path) -> Path:
    cfg = kb_dir / ".engram" / "sources.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text())
        except json.JSONDecodeError:
            data = {}
        g = (data.get("granola") or {}) if isinstance(data, dict) else {}
        cp = g.get("cache_path")
        if isinstance(cp, str) and cp.strip():
            return Path(cp).expanduser()
    return DEFAULT_CACHE_PATH


def _meta_path(kb_dir: Path) -> Path:
    return kb_dir / ".engram" / "granola-sync-meta.json"


def _load_meta(kb_dir: Path) -> dict[str, Any]:
    p = _meta_path(kb_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def _write_meta(kb_dir: Path, meta: dict[str, Any]) -> None:
    p = _meta_path(kb_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2) + "\n")


# ---- cache parsing ---------------------------------------------------------

class GranolaCacheError(RuntimeError):
    pass


def load_cache(cache_path: Path) -> dict[str, Any]:
    """Parse Granola's cache-v3.json, handling both v5 and v6 wrappers.

    Raises GranolaCacheError with a clear message on any format surprise.
    """
    if not cache_path.exists():
        raise GranolaCacheError(
            f"Granola cache not found at {cache_path}. Is Granola installed? "
            f"If the cache lives elsewhere, set granola.cache_path in "
            f".engram/sources.json."
        )
    try:
        outer = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GranolaCacheError(f"cache-v3.json is not valid JSON: {exc}") from exc
    if not isinstance(outer, dict) or "cache" not in outer:
        raise GranolaCacheError("cache-v3.json missing top-level 'cache' key")

    cache_raw = outer["cache"]
    if isinstance(cache_raw, str):
        try:
            inner = json.loads(cache_raw)
        except json.JSONDecodeError as exc:
            raise GranolaCacheError(f"inner 'cache' string is not valid JSON: {exc}") from exc
    elif isinstance(cache_raw, dict):
        inner = cache_raw
    else:
        raise GranolaCacheError(
            f"'cache' key has unexpected type {type(cache_raw).__name__}; "
            f"expected string (pre-v6) or object (v6+)."
        )
    if "state" not in inner:
        raise GranolaCacheError("cache content missing 'state' key")
    return inner["state"]


def _iter_meetings(state: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield one merged meeting record per document, combining documents
    with metadata, transcripts, panels, and folder info.

    Mirrors GranolaMCP's merge logic so we match what community tools
    expect (docs override metadata on conflict; transcript_data and
    ai_summary_html added)."""
    documents = state.get("documents") or {}
    metadata = state.get("meetingsMetadata") or {}
    transcripts = state.get("transcripts") or {}
    panels = state.get("documentPanels") or {}
    lists = state.get("documentLists") or {}
    lists_meta = state.get("documentListsMetadata") or {}

    # Reverse folder index.
    doc_to_folder: dict[str, dict[str, str]] = {}
    for list_id, doc_ids in lists.items():
        folder_name = (lists_meta.get(list_id) or {}).get("title") or "Unknown"
        for did in doc_ids or []:
            doc_to_folder[did] = {"folder_id": list_id, "folder_name": folder_name}

    for doc_id, doc in documents.items():
        if not isinstance(doc, dict):
            continue
        meeting = dict(doc)

        for key, value in (metadata.get(doc_id) or {}).items():
            if key not in meeting or not meeting[key]:
                meeting[key] = value

        if doc_id in transcripts:
            meeting["transcript_data"] = transcripts[doc_id]

        doc_panels = panels.get(doc_id) or {}
        ai_summaries: list[str] = []
        panel_content: dict[str, Any] | None = None
        for pdata in doc_panels.values():
            oc = pdata.get("original_content") if isinstance(pdata, dict) else None
            if isinstance(oc, str) and oc.strip() and not oc.strip().startswith("<hr>"):
                ai_summaries.append(oc)
            if panel_content is None:
                pc = pdata.get("content") if isinstance(pdata, dict) else None
                if isinstance(pc, dict):
                    panel_content = pc
        if ai_summaries:
            meeting["ai_summary_html"] = "\n\n".join(ai_summaries)
        if panel_content is not None:
            meeting["panel_content"] = panel_content

        folder = doc_to_folder.get(doc_id) or {}
        if folder:
            meeting["folder_id"] = folder.get("folder_id")
            meeting["folder_name"] = folder.get("folder_name")

        # Canonical id for downstream code.
        meeting["_resolved_id"] = _pick(meeting, ["id", "meeting_id", "session_id", "uuid"], doc_id)
        yield meeting


def _pick(obj: dict[str, Any], keys: list[str], default: Any = "") -> Any:
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


# ---- field resolution ------------------------------------------------------

def _meeting_title(m: dict[str, Any]) -> str:
    t = _pick(m, ["title", "name", "subject", "meeting_name"], "")
    return str(t).strip() or "(untitled meeting)"


def _meeting_timestamp(m: dict[str, Any]) -> str:
    # Prefer finish/end time for chronology; fall back to start/created.
    ts = _pick(m, [
        "end_time", "endTime", "finished_at",
        "start_time", "startTime",
        "created_at", "timestamp", "date", "updated_at",
    ], "")
    if isinstance(ts, dict):
        # Google-calendar style {"dateTime": "..."} — rare but seen.
        ts = ts.get("dateTime") or ""
    return str(ts) if ts else ""


def _meeting_participants(m: dict[str, Any]) -> list[str]:
    for key in ("participants", "attendees", "users", "members"):
        val = m.get(key)
        if not val:
            continue
        out: list[str] = []
        if isinstance(val, list):
            for entry in val:
                if isinstance(entry, str):
                    out.append(entry)
                elif isinstance(entry, dict):
                    name = _pick(entry, ["name", "display_name", "email", "username"], "")
                    if name:
                        out.append(str(name))
        elif isinstance(val, dict):
            for entry in val.values():
                if isinstance(entry, dict):
                    name = _pick(entry, ["name", "display_name", "email", "username"], "")
                    if name:
                        out.append(str(name))
        if out:
            return out
    return []


def _meeting_duration_minutes(m: dict[str, Any]) -> int | None:
    d = _pick(m, ["duration_seconds", "duration", "length", "meeting_duration"], None)
    if d is None:
        return None
    try:
        secs = float(d)
    except (TypeError, ValueError):
        return None
    # Heuristic: if the number looks like milliseconds, convert down.
    if secs > 60 * 60 * 24:  # > 1 day is almost certainly ms
        secs = secs / 1000
    return max(1, int(round(secs / 60)))


# ---- HTML → markdown-with-headings ----------------------------------------

class _AISummaryParser(HTMLParser):
    """Flatten AI-summary HTML into block dicts compatible with
    chunk_by_headings. H1/H2 become heading blocks; everything else becomes
    paragraph blocks with prefixes for list items."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, str]] = []
        self._buf: list[str] = []
        self._current_block_type: str = "paragraph"
        self._list_stack: list[str] = []  # "ul" or "ol"
        self._ol_counters: list[int] = []
        self._in_li: bool = False

    def _flush(self) -> None:
        text = "".join(self._buf).strip()
        self._buf = []
        if not text:
            self._current_block_type = "paragraph"
            return
        self.blocks.append({"type": self._current_block_type, "plain_text": text})
        self._current_block_type = "paragraph"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in ("h1", "h2", "h3"):
            self._flush()
            self._current_block_type = {
                "h1": "heading_1",
                "h2": "heading_2",
                "h3": "heading_3",
            }[tag]
        elif tag in ("p", "div"):
            self._flush()
            self._current_block_type = "paragraph"
        elif tag == "br":
            self._buf.append("\n")
        elif tag in ("ul", "ol"):
            self._flush()
            self._list_stack.append(tag)
            if tag == "ol":
                self._ol_counters.append(1)
        elif tag == "li":
            self._flush()
            self._in_li = True
            indent = "  " * (len(self._list_stack) - 1) if self._list_stack else ""
            if self._list_stack and self._list_stack[-1] == "ol":
                n = self._ol_counters[-1] if self._ol_counters else 1
                self._buf.append(f"{indent}{n}. ")
                if self._ol_counters:
                    self._ol_counters[-1] += 1
            else:
                self._buf.append(f"{indent}- ")
        elif tag in ("strong", "b"):
            self._buf.append("**")
        elif tag in ("em", "i"):
            self._buf.append("*")
        elif tag == "code":
            self._buf.append("`")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("h1", "h2", "h3"):
            self._flush()
        elif tag in ("p", "div"):
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush()
            if self._list_stack:
                self._list_stack.pop()
            if tag == "ol" and self._ol_counters:
                self._ol_counters.pop()
        elif tag == "li":
            self._flush()
            self._in_li = False
        elif tag in ("strong", "b"):
            self._buf.append("**")
        elif tag in ("em", "i"):
            self._buf.append("*")
        elif tag == "code":
            self._buf.append("`")

    def handle_data(self, data: str) -> None:
        if data:
            self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def _summary_html_to_blocks(html: str) -> list[dict[str, str]]:
    if not html or not html.strip():
        return []
    p = _AISummaryParser()
    try:
        p.feed(html)
    except Exception:
        # If HTML is malformed, fall back to a plain-text single block.
        return [{"type": "paragraph", "plain_text": re.sub(r"<[^>]+>", "", html)}]
    p.close()
    return p.blocks


# ---- ProseMirror → blocks --------------------------------------------------

def _prosemirror_to_blocks(node: Any, depth: int = 0) -> list[dict[str, str]]:
    """Flatten a ProseMirror tree into heading/paragraph blocks."""
    out: list[dict[str, str]] = []
    if not isinstance(node, dict):
        return out
    ntype = node.get("type")
    content = node.get("content") or []

    if ntype == "doc":
        for c in content:
            out.extend(_prosemirror_to_blocks(c, depth))
        return out

    if ntype == "heading":
        level = ((node.get("attrs") or {}).get("level")) or 2
        text = _prosemirror_text(content).strip()
        if level == 1:
            out.append({"type": "heading_1", "plain_text": text})
        elif level == 2:
            out.append({"type": "heading_2", "plain_text": text})
        else:
            out.append({"type": "paragraph", "plain_text": f"**{text}**"})
        return out

    if ntype == "paragraph":
        text = _prosemirror_text(content).strip()
        if text:
            out.append({"type": "paragraph", "plain_text": text})
        return out

    if ntype in ("bullet_list", "bulletList"):
        for li in content:
            li_text = _prosemirror_text(li.get("content") or []).strip()
            if li_text:
                out.append({"type": "paragraph", "plain_text": f"{'  '*depth}- {li_text}"})
        return out

    if ntype in ("ordered_list", "orderedList"):
        for i, li in enumerate(content, 1):
            li_text = _prosemirror_text(li.get("content") or []).strip()
            if li_text:
                out.append({"type": "paragraph", "plain_text": f"{'  '*depth}{i}. {li_text}"})
        return out

    # Unknown container — recurse into children.
    for c in content:
        out.extend(_prosemirror_to_blocks(c, depth))
    return out


def _prosemirror_text(nodes: Any) -> str:
    if not isinstance(nodes, list):
        return ""
    parts: list[str] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        if n.get("type") == "text":
            t = n.get("text") or ""
            marks = {m.get("type") for m in (n.get("marks") or []) if isinstance(m, dict)}
            if "strong" in marks or "bold" in marks:
                t = f"**{t}**"
            if "em" in marks or "italic" in marks:
                t = f"*{t}*"
            if "code" in marks:
                t = f"`{t}`"
            parts.append(t)
        elif n.get("type") == "hard_break":
            parts.append("\n")
        else:
            # Recurse into inline containers (e.g. link wrappers).
            parts.append(_prosemirror_text(n.get("content") or []))
    return "".join(parts)


# ---- transcript helpers ----------------------------------------------------

def _transcript_text(segments: Any) -> str:
    """Concat transcript segments, one line per segment with speaker tag."""
    if not isinstance(segments, list):
        return ""
    lines: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = _pick(seg, ["text", "content"], "").strip()
        if not text:
            continue
        source = seg.get("source")  # "mic" | "system" | None
        speaker = "You" if source == "mic" else ("Other" if source == "system" else None)
        lines.append(f"{speaker}: {text}" if speaker else text)
    return "\n".join(lines)


# ---- main sync -------------------------------------------------------------

def sync(
    kb_dir: Path,
    *,
    full: bool = False,
    cache_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Parse Granola's local cache and return a list of Item JSONs."""
    if sys.platform != "darwin":
        print(
            "[granola] skipped: Granola is macOS-only (platform is "
            f"{sys.platform!r}).",
            file=sys.stderr,
        )
        return []

    cache_path = cache_path or _load_cache_path(kb_dir)
    try:
        state = load_cache(cache_path)
    except GranolaCacheError as exc:
        print(f"[granola] {exc}", file=sys.stderr)
        return []

    meta = _load_meta(kb_dir)
    last_synced_at = None if full else meta.get("last_synced_at")

    known_ids: set[str] = set()
    if last_synced_at:
        for it in load_items(kb_dir / "raw" / "items.jsonl"):
            if it.get("source") == SOURCE_ID:
                mid = (it.get("metadata") or {}).get("meeting_id")
                if mid:
                    known_ids.add(mid)
        print(
            f"[granola] incremental: {len(known_ids)} known meeting(s); "
            f"re-syncing any updated since {last_synced_at} or not yet indexed",
            file=sys.stderr,
        )
    else:
        print(f"[granola] full sync from {cache_path}", file=sys.stderr)

    all_items: list[Item] = []
    seen = 0
    changed = 0
    for meeting in _iter_meetings(state):
        seen += 1
        meeting_id = str(meeting["_resolved_id"])
        # Use the same timestamp ladder as _meeting_timestamp so the
        # incremental filter agrees with what we emit. If a known meeting
        # has no timestamp, assume unchanged and skip.
        updated = _meeting_timestamp(meeting)
        if last_synced_at and meeting_id in known_ids:
            if not updated or str(updated) <= last_synced_at:
                continue

        items = _build_items_for_meeting(meeting)
        if not items:
            continue

        drop_items_by_id_prefix(kb_dir, f"{SOURCE_ID}:{meeting_id}:")
        all_items.extend(items)
        changed += 1

    now = datetime.now(timezone.utc).isoformat()
    _write_meta(kb_dir, {
        "last_synced_at": now,
        "last_run_meetings_seen": seen,
        "last_run_meetings_changed": changed,
        "cache_path": str(cache_path),
    })
    print(
        f"[granola] {seen} meeting(s) seen · {changed} changed · "
        f"{len(all_items)} chunk(s) emitted",
        file=sys.stderr,
    )
    return [it.to_json() for it in all_items]


def _build_items_for_meeting(meeting: dict[str, Any]) -> list[Item]:
    """Pick content source (summary > notes > transcript), chunk, emit Items."""
    meeting_id = str(meeting["_resolved_id"])
    title = _meeting_title(meeting)
    ts = _meeting_timestamp(meeting)
    participants = _meeting_participants(meeting)
    duration = _meeting_duration_minutes(meeting)
    folder = meeting.get("folder_name")

    summary_html = meeting.get("ai_summary_html") or ""
    notes = meeting.get("notes") or meeting.get("panel_content")
    transcript_text = _transcript_text(meeting.get("transcript_data"))

    # Build blocks from the highest-signal available source.
    blocks: list[dict[str, str]] = []
    content_source = ""
    if summary_html.strip():
        blocks = _summary_html_to_blocks(summary_html)
        content_source = "ai_summary"
    elif isinstance(notes, dict) and notes:
        blocks = _prosemirror_to_blocks(notes)
        content_source = "notes"
    elif transcript_text.strip():
        # No structure — fall through to size-based chunking below.
        content_source = "transcript"

    preamble_parts: list[str] = []
    if participants:
        who = ", ".join(participants[:6])
        if len(participants) > 6:
            who += f" (+{len(participants) - 6} more)"
        preamble_parts.append(f"Participants: {who}")
    if duration is not None:
        preamble_parts.append(f"Duration: {duration} min")
    if folder:
        preamble_parts.append(f"Folder: {folder}")
    preamble = " · ".join(preamble_parts)

    base_meta = {
        "meeting_id": meeting_id,
        "meeting_title": title,
        "meeting_date": ts,
        "participants": participants,
        "duration_minutes": duration,
        "folder_name": folder,
        "content_source": content_source,
    }

    if blocks:
        chunks = chunk_by_headings(blocks, heading_levels=("heading_1", "heading_2"))
        # If the summary/notes produced a single giant unnamed chunk, window it.
        if len(chunks) == 1 and chunks[0].title is None and len(chunks[0].body) > 6000:
            chunks = chunk_by_size(chunks[0].body, max_chars=4000)
        # Skip meetings with no real content anywhere.
        if not any(c.body.strip() for c in chunks):
            # If we also have a transcript, fall back to it rather than drop.
            if not transcript_text.strip():
                return []
            blocks = []

    if not blocks:
        # Transcript-only fallback.
        if not transcript_text.strip():
            return []
        chunks = chunk_by_size(transcript_text, max_chars=4000)
        base_meta["content_source"] = "transcript"

    return make_chunk_items(
        source=SOURCE_ID,
        parent_id=meeting_id,
        parent_title=title,
        chunks=chunks,
        author=None,
        url=None,
        timestamp=str(ts) if ts else "",
        base_metadata=base_meta,
        preamble=preamble or None,
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Dump Granola meetings as chunked Items.")
    ap.add_argument("--kb", type=Path, default=Path.cwd())
    ap.add_argument("--cache", type=Path, default=None, help="Override cache-v3.json path")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    items = sync(args.kb, full=args.full, cache_path=args.cache)
    print(f"[granola] {len(items)} item(s) total")
    for it in items[: args.limit]:
        m = it.get("metadata") or {}
        print(
            f"  {it['id']} · {m.get('meeting_title')!r} "
            f"chunk {m.get('chunk_index')}/{m.get('chunk_count')} "
            f"({m.get('content_source')}) · {len(it['text'])} chars"
        )
