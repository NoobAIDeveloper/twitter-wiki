#!/usr/bin/env python3
"""
Sync Twitter/X bookmarks into an engram KB.

Reads existing `<kb>/raw/bookmarks.jsonl` and `<kb>/.engram/sync-meta.json`,
extracts the user's logged-in cookies from a Chromium-family browser, paginates
the X bookmarks GraphQL endpoint until it hits the newest already-known bookmark
(or runs out of pages), merges new records into the JSONL deduped by id, and
writes back.

Usage:
    python3 scripts/sync.py --kb <kb-path> [--browser auto|chrome|brave|edge]
                                          [--full]
                                          [--max-pages N]
                                          [--delay-ms N]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# sibling imports — sync.py lives next to cookies.py and graphql.py
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.base import load_items, merge_items, write_items  # noqa: E402
from sources.x import SOURCE_ID as X_SOURCE_ID, bookmarks_to_items  # noqa: E402


def _kb_paths(kb: Path) -> tuple[Path, Path, Path]:
    """Return (jsonl_path, meta_path, state_dir) for a KB root."""
    kb = kb.expanduser().resolve()
    if not (kb / "CLAUDE.md").exists():
        print(
            f"warning: {kb} doesn't look like an engram KB "
            f"(no CLAUDE.md). Continuing anyway.",
            file=sys.stderr,
        )
    state_dir = kb / ".engram"
    raw_dir = kb / "raw"
    state_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    return raw_dir / "bookmarks.jsonl", state_dir / "sync-meta.json", state_dir


def _items_path(kb: Path) -> Path:
    return kb.expanduser().resolve() / "raw" / "items.jsonl"


def _rewrite_x_items(kb: Path, merged_bookmarks: list[dict]) -> int:
    """Replace all x-sourced items in raw/items.jsonl with the merged X corpus.

    Other sources' items in items.jsonl are preserved untouched. Returns the
    number of X items now in the file.
    """
    items_path = _items_path(kb)
    try:
        existing = load_items(items_path)
    except ValueError as exc:
        print(f"warning: items.jsonl unreadable, rebuilding: {exc}", file=sys.stderr)
        existing = []
    non_x = [it for it in existing if it.get("source") != X_SOURCE_ID]
    x_items = bookmarks_to_items(merged_bookmarks)
    combined, _ = merge_items(non_x, x_items)
    write_items(items_path, combined)
    return len(x_items)


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(
                    f"warning: skipping malformed line in {path}: {exc}",
                    file=sys.stderr,
                )
    return out


def _load_meta(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def _atomic_write(path: Path, data: str) -> None:
    """Write to a temp file in the same dir then rename. Avoids partial writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(data)
        tmp.replace(path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def _newest_id(bookmarks: list[dict]) -> str | None:
    """The bookmark with the largest snowflake id (= most recently posted)."""
    if not bookmarks:
        return None
    try:
        return max(bookmarks, key=lambda b: int(b.get("id") or b.get("tweetId") or 0))[
            "id"
        ]
    except (KeyError, ValueError):
        return None


def _merge(existing: list[dict], new: list[dict]) -> tuple[list[dict], int]:
    """
    Merge new bookmarks into existing. Dedupe by id. Newer record wins on
    collision (richer data from a fresh sync). Returns (merged, num_new_added).
    """
    by_id: dict[str, dict] = {}
    for rec in existing:
        rid = rec.get("id") or rec.get("tweetId")
        if rid:
            by_id[rid] = rec
    added = 0
    for rec in new:
        rid = rec.get("id") or rec.get("tweetId")
        if not rid:
            continue
        if rid not in by_id:
            added += 1
        by_id[rid] = rec
    # Sort newest first by snowflake id
    merged = sorted(
        by_id.values(),
        key=lambda b: int(b.get("id") or b.get("tweetId") or 0),
        reverse=True,
    )
    return merged, added


def sync(
    kb: Path,
    *,
    browser: str = "auto",
    full: bool = False,
    max_pages: int = 200,
    delay_ms: int = 600,
) -> int:
    # Lazy imports — cryptography is only needed for the X source.
    from cookies import extract_twitter_cookies, list_available_browsers
    from graphql import AuthError, FetchOptions, RateLimitError, fetch_bookmarks

    jsonl_path, meta_path, _state_dir = _kb_paths(kb)

    existing = _load_jsonl(jsonl_path)
    meta = _load_meta(meta_path)

    print(f"[sync] KB: {kb}", file=sys.stderr)
    print(
        f"[sync] existing: {len(existing)} bookmarks "
        f"(last sync: {meta.get('lastSyncAt', 'never')})",
        file=sys.stderr,
    )

    stop_at_id: str | None = None
    if not full:
        stop_at_id = _newest_id(existing)
        if stop_at_id:
            print(
                f"[sync] incremental: will stop at existing id={stop_at_id}",
                file=sys.stderr,
            )
    else:
        print("[sync] FULL sync (ignoring existing bookmarks for stop)", file=sys.stderr)

    # Cookies
    available = list_available_browsers()
    print(f"[sync] available browsers: {available or '(none)'}", file=sys.stderr)
    try:
        cookies = extract_twitter_cookies(browser=browser)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    print("[sync] cookies extracted ok", file=sys.stderr)

    # Fetch
    opts = FetchOptions(
        ct0=cookies["ct0"],
        auth_token=cookies["auth_token"],
        max_pages=max_pages,
        page_delay_ms=delay_ms,
        stop_at_id=stop_at_id,
    )

    fetched: list[dict] = []
    try:
        for rec in fetch_bookmarks(opts):
            fetched.append(rec)
    except AuthError as exc:
        print(
            f"error: authentication failed ({exc}). Your X session cookies are "
            f"stale — log out and back into X in your browser, then retry.",
            file=sys.stderr,
        )
        return 4
    except RateLimitError as exc:
        print(
            f"error: rate limited and exhausted retries ({exc}). Try again later.",
            file=sys.stderr,
        )
        return 5
    except KeyboardInterrupt:
        print(
            f"\n[sync] interrupted; got {len(fetched)} bookmarks before exit",
            file=sys.stderr,
        )
        # Fall through and persist what we have

    print(f"[sync] fetched {len(fetched)} records this run", file=sys.stderr)

    merged, added = _merge(existing, fetched)
    print(
        f"[sync] merged: {len(merged)} total ({added} newly added)",
        file=sys.stderr,
    )

    # Persist source-private bookmarks.jsonl (for X incremental sync)
    body = "\n".join(json.dumps(rec, ensure_ascii=False) for rec in merged) + "\n"
    _atomic_write(jsonl_path, body)

    # Persist normalized items.jsonl (what preprocess reads)
    x_item_count = _rewrite_x_items(kb, merged)
    print(f"[sync] items.jsonl: {x_item_count} x-sourced items", file=sys.stderr)

    new_meta = {
        "provider": "twitter",
        "schemaVersion": 1,
        "lastSyncAt": datetime.now(timezone.utc).isoformat(),
        "totalBookmarks": len(merged),
        "lastRunAdded": added,
        "lastRunFetched": len(fetched),
    }
    _atomic_write(meta_path, json.dumps(new_meta, indent=2) + "\n")

    # User-facing summary on stdout (for the slash command to surface).
    # Use os.path.relpath when the jsonl lives inside kb; otherwise fall
    # back to the absolute path. Symlink chains like /tmp → /private/tmp
    # on macOS would otherwise produce walk-up paths like ../../private/...
    import os
    try:
        rel = os.path.relpath(jsonl_path, kb)
        if rel.startswith(".."):
            rel = str(jsonl_path)
    except ValueError:
        rel = str(jsonl_path)
    print(f"sync complete: {added} new, {len(merged)} total → {rel}")
    return 0


# ---- Source dispatcher -----------------------------------------------------

def _sync_kindle(kb: Path, clippings: Path | None) -> int:
    from sources import kindle
    from sources.base import replace_source_items

    if clippings is None:
        print(
            "error: kindle source requires --clippings <path-to-My Clippings.txt>",
            file=sys.stderr,
        )
        return 64

    print(f"[sync] source=kindle · parsing {clippings}", file=sys.stderr)
    try:
        items = kindle.sync(kb, clippings_path=clippings)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    total, this_source = replace_source_items(kb, kindle.SOURCE_ID, items)
    print(
        f"[sync] kindle: {this_source} highlight(s) · items.jsonl now has "
        f"{total} total",
        file=sys.stderr,
    )
    print(f"sync complete: kindle → {this_source} items")
    return 0


def _sync_github_stars(kb: Path) -> int:
    from sources import github_stars
    from sources.base import replace_source_items
    import os

    handle = github_stars._load_handle(kb)
    if not handle:
        print(
            "[sync] source=github-stars · no handle configured; skipping. "
            "Add to sources.json: {\"github\": {\"handle\": \"yourname\"}}",
            file=sys.stderr,
        )
        return 0

    print(f"[sync] source=github-stars · fetching stars for @{handle}", file=sys.stderr)
    token = os.environ.get("GITHUB_TOKEN")
    try:
        items = github_stars.sync(kb, handle=handle, token=token)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    total, this_source = replace_source_items(kb, github_stars.SOURCE_ID, items)
    print(
        f"[sync] github-stars: {this_source} repo(s) · items.jsonl now has "
        f"{total} total",
        file=sys.stderr,
    )
    print(f"sync complete: github-stars → {this_source} items")
    return 0


def _sync_browser_bookmarks(kb: Path) -> int:
    from sources import browser_bookmarks
    from sources.base import replace_source_items

    print("[sync] source=browser-bookmarks · reading local Bookmarks files", file=sys.stderr)
    items = browser_bookmarks.sync(kb)
    total, this_source = replace_source_items(kb, browser_bookmarks.SOURCE_ID, items)
    print(
        f"[sync] browser-bookmarks: {this_source} URL(s) · items.jsonl now "
        f"has {total} total",
        file=sys.stderr,
    )
    print(f"sync complete: browser-bookmarks → {this_source} items")
    return 0


def _sync_chatgpt(kb: Path, *, browser: str = "auto", full: bool = False) -> int:
    from sources import chatgpt
    from sources.base import load_items, merge_items, replace_source_items

    print("[sync] source=chatgpt · extracting session cookie", file=sys.stderr)
    try:
        new_items = chatgpt.sync(kb, browser=browser, full=full)
    except chatgpt.ChatGPTBlockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "hint: run /kb-request-chatgpt-export to receive an export email, "
            "then /kb-import-chatgpt <path> to ingest it.",
            file=sys.stderr,
        )
        print("sync failed: chatgpt → cloudflare block")
        return 6
    except chatgpt.ChatGPTAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "hint: if this keeps happening, use /kb-import-chatgpt with an export zip.",
            file=sys.stderr,
        )
        print("sync failed: chatgpt → auth error")
        return 4
    except chatgpt.ChatGPTRateLimitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("sync failed: chatgpt → rate limited")
        return 5
    # Incremental: chatgpt.sync only returns new/updated convs. Preserve
    # previously-synced chatgpt items by merging before we replace.
    existing = [
        it for it in load_items(kb / "raw" / "items.jsonl")
        if it.get("source") == chatgpt.SOURCE_ID
    ]
    merged, _ = merge_items(existing, new_items)
    total, this_source = replace_source_items(kb, chatgpt.SOURCE_ID, merged)
    print(
        f"[sync] chatgpt: {this_source} Q+A pair(s) · items.jsonl now has "
        f"{total} total",
        file=sys.stderr,
    )
    print(f"sync complete: chatgpt → {this_source} items")
    return 0


def _sync_claude_ai(kb: Path, *, browser: str = "auto", full: bool = False) -> int:
    from sources import claude_ai
    from sources.base import load_items, merge_items, replace_source_items

    print("[sync] source=claude-ai · extracting session cookie", file=sys.stderr)
    try:
        new_items = claude_ai.sync(kb, browser=browser, full=full)
    except claude_ai.ClaudeAIBlockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "hint: export from Settings → Account → Export Account Data, "
            "then run /kb-import-claude <path> to ingest it.",
            file=sys.stderr,
        )
        print("sync failed: claude-ai → cloudflare block")
        return 6
    except claude_ai.ClaudeAIAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "hint: if this keeps happening, use /kb-import-claude with an export zip.",
            file=sys.stderr,
        )
        print("sync failed: claude-ai → auth error")
        return 4
    except claude_ai.ClaudeAIRateLimitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("sync failed: claude-ai → rate limited")
        return 5
    existing = [
        it for it in load_items(kb / "raw" / "items.jsonl")
        if it.get("source") == claude_ai.SOURCE_ID
    ]
    merged, _ = merge_items(existing, new_items)
    total, this_source = replace_source_items(kb, claude_ai.SOURCE_ID, merged)
    print(
        f"[sync] claude-ai: {this_source} Q+A pair(s) · items.jsonl now has "
        f"{total} total",
        file=sys.stderr,
    )
    print(f"sync complete: claude-ai → {this_source} items")
    return 0


def _sync_notion(kb: Path, *, full: bool = False) -> int:
    from sources import notion
    from sources.base import replace_source_items

    token = notion._load_token(kb)
    if not token:
        print(
            "[sync] source=notion · no token configured; skipping. "
            "Add to sources.json: {\"notion\": {\"token\": \"secret_...\"}}",
            file=sys.stderr,
        )
        return 0

    print("[sync] source=notion · fetching pages via v1 REST API", file=sys.stderr)
    try:
        items = notion.sync(kb, full=full)
    except notion.NotionAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("sync failed: notion → auth error")
        return 4
    except notion.NotionRateLimitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("sync failed: notion → rate limited")
        return 5

    # Incremental: notion.sync only emits items for changed pages, and has
    # already dropped their old chunks from items.jsonl. Merge the fresh
    # chunks with everything else previously known for this source.
    from sources.base import load_items, merge_items
    existing = [
        it for it in load_items(kb / "raw" / "items.jsonl")
        if it.get("source") == notion.SOURCE_ID
    ]
    merged, _ = merge_items(existing, items)
    total, this_source = replace_source_items(kb, notion.SOURCE_ID, merged)
    print(
        f"[sync] notion: {this_source} chunk(s) · items.jsonl now has {total} total",
        file=sys.stderr,
    )
    print(f"sync complete: notion → {this_source} items")
    return 0


def _sync_granola(kb: Path, *, full: bool = False) -> int:
    from sources import granola
    from sources.base import load_items, merge_items, replace_source_items

    print("[sync] source=granola · reading local cache", file=sys.stderr)
    items = granola.sync(kb, full=full)

    # Incremental: granola.sync only returns items for changed meetings and
    # has already dropped their old chunks. Merge with any untouched
    # meetings' chunks still in items.jsonl.
    existing = [
        it for it in load_items(kb / "raw" / "items.jsonl")
        if it.get("source") == granola.SOURCE_ID
    ]
    merged, _ = merge_items(existing, items)
    total, this_source = replace_source_items(kb, granola.SOURCE_ID, merged)
    print(
        f"[sync] granola: {this_source} chunk(s) · items.jsonl now has {total} total",
        file=sys.stderr,
    )
    print(f"sync complete: granola → {this_source} items")
    return 0


def _sync_claude_code(kb: Path, *, include_self: bool = False) -> int:
    from sources import claude_code
    from sources.base import replace_source_items

    print(f"[sync] source=claude-code · scanning {claude_code.PROJECTS_DIR}", file=sys.stderr)
    items = claude_code.sync(kb, include_self=include_self)
    total, this_source = replace_source_items(kb, claude_code.SOURCE_ID, items)
    print(
        f"[sync] claude-code: {this_source} Q+A pair(s) · items.jsonl now "
        f"has {total} total",
        file=sys.stderr,
    )
    print(f"sync complete: claude-code → {this_source} items")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync data from one or more sources into a personal-wiki KB."
    )
    parser.add_argument(
        "--kb",
        type=Path,
        default=Path.cwd(),
        help="Path to the KB root (default: current working directory)",
    )
    parser.add_argument(
        "--source",
        default="x",
        help="Source to sync: x (default), claude-code, chatgpt, claude-ai, notion, granola, browser-bookmarks, github-stars, kindle, all",
    )
    parser.add_argument(
        "--browser",
        default="auto",
        help="(x) Browser to extract cookies from (auto, chrome, brave, edge)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="(x) Full sync — don't stop at the most recent existing bookmark",
    )
    parser.add_argument("--max-pages", type=int, default=200, help="(x) pagination cap")
    parser.add_argument("--delay-ms", type=int, default=600, help="(x) delay between pages")
    parser.add_argument(
        "--include-self",
        action="store_true",
        help="(claude-code) Include Claude Code sessions from the KB's own directory.",
    )
    parser.add_argument(
        "--clippings",
        type=Path,
        default=None,
        help="(kindle) Path to My Clippings.txt",
    )
    args = parser.parse_args()

    sources_to_run = (
        ["x", "claude-code", "chatgpt", "claude-ai", "notion", "granola", "browser-bookmarks", "github-stars"]
        if args.source == "all"
        else [args.source]
    )

    exit_code = 0
    for src in sources_to_run:
        try:
            if src == "x":
                rc = sync(
                    args.kb,
                    browser=args.browser,
                    full=args.full,
                    max_pages=args.max_pages,
                    delay_ms=args.delay_ms,
                )
            elif src == "claude-code":
                rc = _sync_claude_code(args.kb, include_self=args.include_self)
            elif src == "chatgpt":
                rc = _sync_chatgpt(args.kb, browser=args.browser, full=args.full)
            elif src == "claude-ai":
                rc = _sync_claude_ai(args.kb, browser=args.browser, full=args.full)
            elif src == "notion":
                rc = _sync_notion(args.kb, full=args.full)
            elif src == "granola":
                rc = _sync_granola(args.kb, full=args.full)
            elif src == "browser-bookmarks":
                rc = _sync_browser_bookmarks(args.kb)
            elif src == "github-stars":
                rc = _sync_github_stars(args.kb)
            elif src == "kindle":
                # Skip silently in --source all mode if no clippings path.
                if args.source == "all" and args.clippings is None:
                    print(
                        "[sync] source=kindle · skipped (one-shot; provide --clippings to run)",
                        file=sys.stderr,
                    )
                    continue
                rc = _sync_kindle(args.kb, args.clippings)
            else:
                print(f"error: unknown source {src!r}", file=sys.stderr)
                rc = 64
        except Exception as exc:  # noqa: BLE001 — isolate per-source failures
            print(f"error: source {src!r} crashed: {exc}", file=sys.stderr)
            rc = 1
        if rc != 0:
            exit_code = rc
            # Keep going with other sources even if one fails.
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
