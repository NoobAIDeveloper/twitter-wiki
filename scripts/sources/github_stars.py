#!/usr/bin/env python3
"""
GitHub stars source adapter.

Hits the public `GET /users/{user}/starred` endpoint (no auth required for
public stars) and emits one Item per starred repo.

Configuration: the user's GitHub handle is read from
`<kb>/.twitter-wiki/sources.json` under the key `github.handle`. If that
file doesn't exist or doesn't specify a handle, this adapter is a no-op.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .base import Item


SOURCE_ID = "github-stars"
API = "https://api.github.com"
PAGE_SIZE = 100


def _load_handle(kb_dir: Path) -> str | None:
    cfg = kb_dir / ".twitter-wiki" / "sources.json"
    if not cfg.exists():
        return None
    try:
        data = json.loads(cfg.read_text())
    except json.JSONDecodeError:
        return None
    gh = (data.get("github") or {}) if isinstance(data, dict) else {}
    handle = gh.get("handle")
    return handle.strip().lstrip("@") if isinstance(handle, str) and handle.strip() else None


def _fetch_page(handle: str, page: int, token: str | None) -> list[dict[str, Any]]:
    url = f"{API}/users/{handle}/starred?per_page={PAGE_SIZE}&page={page}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github.v3.star+json",
            "User-Agent": "personal-wiki",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sync(kb_dir: Path, *, handle: str | None = None, token: str | None = None) -> list[dict[str, Any]]:
    """Fetch all starred repos for the configured GitHub handle."""
    if handle is None:
        handle = _load_handle(kb_dir)
    if not handle:
        return []

    all_items: list[Item] = []
    for page in range(1, 50):  # hard cap: 5000 stars
        try:
            page_data = _fetch_page(handle, page, token)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError(
                    f"GitHub returned 404 for user {handle!r}. "
                    f"Check the handle in sources.json."
                )
            if exc.code == 403:
                raise RuntimeError(
                    "GitHub rate-limited us. Set a personal access token "
                    "in sources.json under github.token to raise the limit."
                )
            raise
        if not page_data:
            break

        for entry in page_data:
            # With the v3.star+json Accept header, entries are
            # {"starred_at": ..., "repo": {...}}.
            if "repo" in entry:
                repo = entry.get("repo") or {}
                starred_at = entry.get("starred_at") or ""
            else:
                repo = entry
                starred_at = ""
            full_name = repo.get("full_name") or ""
            if not full_name:
                continue
            description = (repo.get("description") or "").strip()
            topics = repo.get("topics") or []
            lang = repo.get("language") or ""

            text_parts = [full_name]
            if description:
                text_parts.append(description)
            if topics:
                text_parts.append("Topics: " + ", ".join(topics))
            if lang:
                text_parts.append(f"Language: {lang}")

            all_items.append(
                Item(
                    id=f"{SOURCE_ID}:{repo.get('id') or full_name}",
                    source=SOURCE_ID,
                    text="\n".join(text_parts),
                    timestamp=starred_at,
                    author=(repo.get("owner") or {}).get("login"),
                    url=repo.get("html_url"),
                    engagement={"stars": repo.get("stargazers_count") or 0},
                    media=[],
                    metadata={
                        "repo": full_name,
                        "language": lang,
                        "topics": topics,
                        "archived": bool(repo.get("archived")),
                    },
                )
            )

        if len(page_data) < PAGE_SIZE:
            break
        time.sleep(0.3)  # be polite

    return [it.to_json() for it in all_items]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Dump GitHub stars as Items.")
    ap.add_argument("--kb", type=Path, default=Path.cwd())
    ap.add_argument("--handle", help="Override handle from sources.json")
    ap.add_argument("--token", help="Optional GitHub PAT (env GITHUB_TOKEN also works)")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    import os
    token = args.token or os.environ.get("GITHUB_TOKEN")

    items = sync(args.kb, handle=args.handle, token=token)
    print(f"[github-stars] {len(items)} starred repo(s)")
    for it in items[: args.limit]:
        print(f"  {it['metadata']['repo']} · ⭐{it['engagement']['stars']} · {it['metadata']['language']}")
