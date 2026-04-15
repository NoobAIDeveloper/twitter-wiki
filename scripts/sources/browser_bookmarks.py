#!/usr/bin/env python3
"""
Browser bookmarks source adapter.

Reads the Chrome/Brave/Edge `Bookmarks` JSON file (same profile dir we
already know about from cookies.py), flattens the folder tree, and emits
one Item per saved URL.

No auth, no network. Just a local file read.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# Sibling import — this module lives next to cookies.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cookies import BROWSERS, _user_data_dir  # noqa: E402

from .base import Item


SOURCE_ID = "browser-bookmarks"

# Chrome uses WebKit time: microseconds since 1601-01-01 UTC.
_WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _webkit_to_iso(value: str | int | None) -> str:
    if not value:
        return ""
    try:
        us = int(value)
    except (TypeError, ValueError):
        return ""
    if us == 0:
        return ""
    try:
        dt = _WEBKIT_EPOCH + timedelta(microseconds=us)
        return dt.isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError):
        return ""


def _bookmarks_file(browser_id: str, profile: str = "Default") -> Path:
    for b in BROWSERS:
        if b.id == browser_id:
            return _user_data_dir(b) / profile / "Bookmarks"
    raise ValueError(f"unknown browser {browser_id!r}")


def _walk(node: dict[str, Any], folder_path: str) -> Iterable[dict[str, Any]]:
    if node.get("type") == "url":
        yield {
            "name": node.get("name") or "",
            "url": node.get("url") or "",
            "date_added": node.get("date_added"),
            "folder": folder_path,
            "guid": node.get("guid") or "",
        }
    for child in node.get("children") or []:
        sub = folder_path
        if node.get("name"):
            sub = f"{folder_path}/{node['name']}" if folder_path else node["name"]
        yield from _walk(child, sub)


def _collect(browser_id: str) -> list[Item]:
    path = _bookmarks_file(browser_id)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    roots = data.get("roots") or {}

    items: list[Item] = []
    for root_name, root in roots.items():
        if not isinstance(root, dict):
            continue
        for entry in _walk(root, root_name):
            if not entry["url"]:
                continue
            # Skip internal URLs — chrome://, javascript:, about:blank, etc.
            if entry["url"].startswith(("chrome://", "edge://", "brave://", "javascript:", "about:")):
                continue
            text_parts: list[str] = []
            if entry["name"]:
                text_parts.append(entry["name"])
            text_parts.append(entry["url"])
            items.append(
                Item(
                    id=f"{SOURCE_ID}:{browser_id}:{entry['guid'] or entry['url']}",
                    source=SOURCE_ID,
                    text="\n".join(text_parts),
                    timestamp=_webkit_to_iso(entry["date_added"]),
                    author=None,
                    url=entry["url"],
                    engagement=None,
                    media=[],
                    metadata={
                        "title": entry["name"],
                        "folder": entry["folder"],
                        "browser": browser_id,
                    },
                )
            )
    return items


def sync(kb_dir: Path | None = None, *, browsers: list[str] | None = None) -> list[dict[str, Any]]:
    """Collect bookmarks from the configured browsers.

    Defaults to all installed browsers whose Bookmarks file exists.
    """
    if browsers is None:
        browsers = []
        for b in BROWSERS:
            try:
                if _bookmarks_file(b.id).exists():
                    browsers.append(b.id)
            except (ValueError, NotImplementedError):
                continue

    all_items: list[Item] = []
    for bid in browsers:
        try:
            all_items.extend(_collect(bid))
        except (OSError, json.JSONDecodeError):
            continue
    return [it.to_json() for it in all_items]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Dump browser bookmarks as Items.")
    ap.add_argument("--browser", action="append", help="Restrict to one browser id")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    items = sync(browsers=args.browser)
    print(f"[browser-bookmarks] {len(items)} URL(s)")
    for it in items[: args.limit]:
        print(f"  {it['metadata']['browser']} · {it['metadata']['folder']} · {it['metadata']['title'][:60]}")
