#!/usr/bin/env python3
"""
Sync Twitter/X bookmarks into a twitter-wiki KB.

Reads existing `<kb>/raw/bookmarks.jsonl` and `<kb>/.twitter-wiki/sync-meta.json`,
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

from cookies import extract_twitter_cookies, list_available_browsers  # noqa: E402
from graphql import (  # noqa: E402
    AuthError,
    FetchOptions,
    RateLimitError,
    fetch_bookmarks,
)


def _kb_paths(kb: Path) -> tuple[Path, Path, Path]:
    """Return (jsonl_path, meta_path, state_dir) for a KB root."""
    kb = kb.expanduser().resolve()
    if not (kb / "CLAUDE.md").exists():
        print(
            f"warning: {kb} doesn't look like a twitter-wiki KB "
            f"(no CLAUDE.md). Continuing anyway.",
            file=sys.stderr,
        )
    state_dir = kb / ".twitter-wiki"
    raw_dir = kb / "raw"
    state_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    return raw_dir / "bookmarks.jsonl", state_dir / "sync-meta.json", state_dir


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

    # Persist
    body = "\n".join(json.dumps(rec, ensure_ascii=False) for rec in merged) + "\n"
    _atomic_write(jsonl_path, body)

    new_meta = {
        "provider": "twitter",
        "schemaVersion": 1,
        "lastSyncAt": datetime.now(timezone.utc).isoformat(),
        "totalBookmarks": len(merged),
        "lastRunAdded": added,
        "lastRunFetched": len(fetched),
    }
    _atomic_write(meta_path, json.dumps(new_meta, indent=2) + "\n")

    # User-facing summary on stdout (for the slash command to surface)
    print(
        f"sync complete: {added} new, {len(merged)} total → "
        f"{jsonl_path.relative_to(kb)}"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Twitter/X bookmarks into a twitter-wiki KB."
    )
    parser.add_argument(
        "--kb",
        type=Path,
        default=Path.cwd(),
        help="Path to the KB root (default: current working directory)",
    )
    parser.add_argument(
        "--browser",
        default="auto",
        help="Browser to extract cookies from (auto, chrome, brave, edge)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Full sync — don't stop at the most recent existing bookmark",
    )
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--delay-ms", type=int, default=600)
    args = parser.parse_args()

    code = sync(
        args.kb,
        browser=args.browser,
        full=args.full,
        max_pages=args.max_pages,
        delay_ms=args.delay_ms,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
