#!/usr/bin/env python3
"""
Import an official ChatGPT or Claude.ai export zip into a KB.

Used as the fallback when the live cookie-based sync breaks (rotated
endpoints, Cloudflare challenges, etc.). Merges the extracted Items
into raw/items.jsonl, preserving other sources.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.base import load_items, merge_items, replace_source_items  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Import a ChatGPT or Claude.ai export zip into a KB."
    )
    ap.add_argument("--provider", required=True, choices=["chatgpt", "claude-ai"])
    ap.add_argument("--zip", required=True, type=Path, help="Path to the export zip")
    ap.add_argument("--kb", type=Path, default=Path.cwd())
    args = ap.parse_args()

    if args.provider == "chatgpt":
        from sources import chatgpt as src
    else:
        from sources import claude_ai as src

    try:
        new_items = src.ingest_export(args.zip)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except zipfile.BadZipFile:
        print(
            f"error: {args.zip} is not a valid zip file. Check that the "
            f"download completed (no .part suffix) and that you pointed "
            f"at the zip OpenAI/Anthropic emailed you.",
            file=sys.stderr,
        )
        sys.exit(3)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(3)

    existing = [
        it for it in load_items(args.kb / "raw" / "items.jsonl")
        if it.get("source") == src.SOURCE_ID
    ]
    merged, added = merge_items(existing, new_items)
    total, this_source = replace_source_items(args.kb, src.SOURCE_ID, merged)
    print(
        f"import complete: {args.provider} → {this_source} Q+A pair(s) "
        f"({added} new). items.jsonl now has {total} total."
    )


if __name__ == "__main__":
    main()
