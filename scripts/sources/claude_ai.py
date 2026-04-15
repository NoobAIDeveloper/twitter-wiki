#!/usr/bin/env python3
"""
Claude.ai source adapter.

Same pattern as the ChatGPT adapter: pulls the logged-in `sessionKey`
cookie (plus any Cloudflare cookies the browser holds) from the user's
browser, lists conversations from the user's organization(s), fetches
per-conversation message trees, and emits one Item per user+assistant
Q+A turn pair.

Fragile path: Claude.ai sits behind Cloudflare. We pass along
`cf_clearance` when present and send Chromium-shaped headers. If CF
still blocks, the fallback is the manual export (Settings → Account →
Export) routed through `/kb-import-claude`.
"""

from __future__ import annotations

import json
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cookies import extract_cookies  # noqa: E402

from sources._cfbrowser import (  # noqa: E402
    CLOUDFLARE_OPTIONAL_COOKIES,
    browser_headers,
    looks_like_cf_block,
)
from sources.base import Item  # noqa: E402


SOURCE_ID = "claude-ai"
HOST_PATTERNS = ("%claude.ai",)
SESSION_COOKIE = "sessionKey"
COOKIE_NAMES = {SESSION_COOKIE}
BASE = "https://claude.ai"

MAX_USER_LEN = 4000
MAX_ASSISTANT_LEN = 4000
PAGE_SIZE = 30
MAX_CONVERSATIONS = 1000


class ClaudeAIAuthError(RuntimeError):
    pass


class ClaudeAIRateLimitError(RuntimeError):
    def __init__(self, message: str, retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ClaudeAIBlockedError(RuntimeError):
    """Cloudflare is serving a challenge page instead of the real API.
    Orthogonal to cookie validity — the fix is to revisit claude.ai in the
    browser, wait, or use the export-zip path."""
    pass


def _get_cookies(browser: str = "auto") -> dict[str, str]:
    cookies = extract_cookies(
        host_patterns=HOST_PATTERNS,
        wanted_names=COOKIE_NAMES,
        optional_names=CLOUDFLARE_OPTIONAL_COOKIES,
        browser=browser,
        site_label="claude.ai",
    )
    if SESSION_COOKIE not in cookies:
        raise ClaudeAIAuthError(
            "Missing sessionKey cookie. Log into https://claude.ai in your "
            "browser and retry."
        )
    return cookies


def _request(url: str, cookies: dict[str, str]) -> Any:
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = browser_headers(BASE, extra={"Cookie": cookie_header})
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        if exc.code == 403 and looks_like_cf_block(raw):
            raise ClaudeAIBlockedError(
                "Claude.ai is behind a Cloudflare challenge. Open "
                "https://claude.ai in the same browser (which refreshes "
                "cf_clearance), wait 30+ minutes if the block persists, or "
                "export your data from Settings → Account → Export Account "
                "Data and run `/kb-import-claude <path-to-zip>`."
            ) from exc
        snippet = raw[:300]
        if exc.code in (401, 403):
            raise ClaudeAIAuthError(
                f"Claude.ai returned {exc.code}. Your session cookie is stale — "
                f"log out and back into claude.ai and retry. Body: {snippet}"
            ) from exc
        if exc.code == 429:
            try:
                retry_after = int(exc.headers.get("Retry-After", "60"))
            except (TypeError, ValueError):
                retry_after = 60
            retry_after = max(10, min(retry_after, 300))
            raise ClaudeAIRateLimitError(
                f"Claude.ai rate-limited us; Retry-After={retry_after}s.",
                retry_after=retry_after,
            ) from exc
        raise RuntimeError(f"HTTP {exc.code} from {url}: {snippet}") from exc


def _list_organizations(cookies: dict[str, str]) -> list[str]:
    data = _request(f"{BASE}/api/organizations", cookies)
    if not isinstance(data, list):
        raise ClaudeAIAuthError(
            "Unexpected response from /api/organizations — session may be invalid."
        )
    org_ids = [o.get("uuid") for o in data if o.get("uuid")]
    if not org_ids:
        raise ClaudeAIAuthError("No organizations found on this Claude.ai account.")
    return org_ids


def _list_conversations(
    cookies: dict[str, str],
    org_id: str,
    *,
    stop_at_update_time: str | None,
    hard_limit: int = MAX_CONVERSATIONS,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while offset < hard_limit:
        url = (
            f"{BASE}/api/organizations/{org_id}/chat_conversations"
            f"?limit={PAGE_SIZE}&offset={offset}"
        )
        data = _request(url, cookies)
        items = data if isinstance(data, list) else (data.get("items") or [])
        if not items:
            break
        # Normalize: sort by updated_at desc so stop-at works reliably.
        items.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
        reached_known = False
        for conv in items:
            if stop_at_update_time and (conv.get("updated_at") or "") <= stop_at_update_time:
                reached_known = True
                break
            out.append(conv)
        if reached_known or len(items) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.3)
    return out


def _fetch_conversation(
    cookies: dict[str, str], org_id: str, conv_uuid: str
) -> dict[str, Any]:
    url = (
        f"{BASE}/api/organizations/{org_id}/chat_conversations/{conv_uuid}"
        f"?tree=True&rendering_mode=messages"
    )
    return _request(url, cookies)


def _message_text(msg: dict[str, Any]) -> str:
    # Newer responses expose a `content` list of {type, text} blocks;
    # older ones expose a flat `text` field. Handle both.
    content = msg.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                parts.append(block["text"])
        if parts:
            return "\n".join(parts)
    t = msg.get("text")
    return t if isinstance(t, str) else ""


def _pair_turns(conv: dict[str, Any]) -> list[Item]:
    conv_uuid = conv.get("uuid") or ""
    title = conv.get("name") or "(untitled)"
    messages = conv.get("chat_messages") or []
    # chat_messages are chronological already, but sort defensively.
    messages = sorted(messages, key=lambda m: m.get("created_at") or "")

    items: list[Item] = []
    turn_index = 0
    i = 0
    while i < len(messages):
        m = messages[i]
        if (m.get("sender") or "").lower() != "human":
            i += 1
            continue
        user_text = _message_text(m).strip()
        if not user_text:
            i += 1
            continue
        if len(user_text) > MAX_USER_LEN:
            user_text = user_text[:MAX_USER_LEN] + "\n[...truncated]"

        assistant_parts: list[str] = []
        j = i + 1
        while j < len(messages):
            mj = messages[j]
            sender = (mj.get("sender") or "").lower()
            if sender == "human":
                break
            if sender == "assistant":
                t = _message_text(mj).strip()
                if t:
                    assistant_parts.append(t)
            j += 1

        assistant_text = "\n\n".join(assistant_parts).strip()
        if len(assistant_text) > MAX_ASSISTANT_LEN:
            assistant_text = assistant_text[:MAX_ASSISTANT_LEN] + "\n[...truncated]"

        if len(user_text) < 20 and len(assistant_text) < 100:
            i = j
            continue

        ts = m.get("created_at") or ""
        body = (
            f"**User:** {user_text}\n\n**Claude:** {assistant_text}"
            if assistant_text
            else f"**User:** {user_text}"
        )
        items.append(
            Item(
                id=f"{SOURCE_ID}:{conv_uuid}:{turn_index}",
                source=SOURCE_ID,
                text=body,
                timestamp=ts,
                author=None,
                url=f"{BASE}/chat/{conv_uuid}" if conv_uuid else None,
                engagement=None,
                media=[],
                metadata={
                    "conversation_id": conv_uuid,
                    "conversation_title": title,
                    "turn_index": turn_index,
                },
            )
        )
        turn_index += 1
        i = j

    return items


def sync(
    kb_dir: Path,
    *,
    browser: str = "auto",
    full: bool = False,
) -> list[dict[str, Any]]:
    state_dir = kb_dir / ".twitter-wiki"
    state_dir.mkdir(parents=True, exist_ok=True)
    meta_path = state_dir / "claude-ai-sync-meta.json"

    stop_at: str | None = None
    if not full and meta_path.exists():
        try:
            stop_at = json.loads(meta_path.read_text()).get("lastUpdateTime")
        except json.JSONDecodeError:
            stop_at = None

    cookies = _get_cookies(browser=browser)
    print("[claude-ai] session key ok", file=sys.stderr)

    org_ids = _list_organizations(cookies)
    print(f"[claude-ai] {len(org_ids)} organization(s)", file=sys.stderr)

    all_conv_summaries: list[tuple[str, dict[str, Any]]] = []
    for org_id in org_ids:
        conv_list = _list_conversations(cookies, org_id, stop_at_update_time=stop_at)
        for c in conv_list:
            all_conv_summaries.append((org_id, c))

    eta_min = max(1, round(len(all_conv_summaries) * 5.5 / 60))
    print(
        f"[claude-ai] {len(all_conv_summaries)} new/updated conversation(s); "
        f"ETA ~{eta_min} min (rate-limited retries are automatic)",
        file=sys.stderr,
    )

    # Oldest-first so the cursor advances monotonically on partial progress.
    all_conv_summaries.sort(key=lambda pair: pair[1].get("updated_at") or "")

    all_items: list[Item] = []
    newest_update_time = stop_at or ""
    halt_msg: str | None = None
    for i, (org_id, summary) in enumerate(all_conv_summaries, 1):
        conv_uuid = summary.get("uuid")
        if not conv_uuid:
            continue
        conv: dict[str, Any] | None = None
        skip = False
        for attempt in range(3):
            try:
                conv = _fetch_conversation(cookies, org_id, conv_uuid)
                break
            except ClaudeAIAuthError:
                raise
            except ClaudeAIBlockedError:
                halt_msg = (
                    f"Cloudflare block at {i - 1}/{len(all_conv_summaries)}; "
                    f"progress saved. Revisit claude.ai in your browser or export "
                    f"from Settings → Account and use /kb-import-claude."
                )
                break
            except ClaudeAIRateLimitError as exc:
                wait = exc.retry_after + random.uniform(0, 5)
                print(
                    f"[claude-ai] rate-limited at {i}/{len(all_conv_summaries)}; "
                    f"sleeping {wait:.0f}s (attempt {attempt + 1}/3)",
                    file=sys.stderr,
                )
                time.sleep(wait)
            except Exception as exc:  # noqa: BLE001
                print(f"[claude-ai] skip conversation {conv_uuid}: {exc}", file=sys.stderr)
                skip = True
                break
        if halt_msg:
            break
        if skip:
            continue
        if conv is None:
            halt_msg = (
                f"still rate-limited after 3 retries at "
                f"{i - 1}/{len(all_conv_summaries)}; progress saved. Rerun "
                f"/kb-sync --source claude-ai in ~30 min to pick up the "
                f"remaining {len(all_conv_summaries) - i + 1}."
            )
            break
        all_items.extend(_pair_turns(conv))
        ut = summary.get("updated_at") or ""
        if ut > newest_update_time:
            newest_update_time = ut
        if i % 10 == 0:
            print(f"[claude-ai] fetched {i}/{len(all_conv_summaries)}", file=sys.stderr)
        time.sleep(random.uniform(4.0, 7.0))

    if newest_update_time:
        meta_path.write_text(
            json.dumps({"lastUpdateTime": newest_update_time}, indent=2) + "\n"
        )

    if halt_msg:
        print(f"[claude-ai] {halt_msg}", file=sys.stderr)

    return [it.to_json() for it in all_items]


def ingest_export(zip_path: Path) -> list[dict[str, Any]]:
    """Parse an official Claude.ai export zip and emit Items.

    The export contains `conversations.json` with entries shaped like the
    API's per-conversation response (uuid, name, chat_messages), so we
    reuse `_pair_turns` directly.
    """
    import zipfile

    if not zip_path.exists():
        raise FileNotFoundError(f"Export zip not found: {zip_path}")

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        target = next(
            (n for n in names if n.endswith("conversations.json")),
            None,
        )
        if target is None:
            raise ValueError(
                f"{zip_path.name} does not contain conversations.json — is "
                f"this a Claude.ai export?"
            )
        raw = zf.read(target)

    conversations = json.loads(raw.decode("utf-8"))
    if not isinstance(conversations, list):
        raise ValueError("conversations.json is not a list — unexpected format.")

    items: list[Item] = []
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        items.extend(_pair_turns(conv))
    return [it.to_json() for it in items]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Sync Claude.ai conversations as Items.")
    ap.add_argument("--kb", type=Path, default=Path.cwd())
    ap.add_argument("--browser", default="auto")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    items = sync(args.kb, browser=args.browser, full=args.full)
    print(f"[claude-ai] {len(items)} Q+A pair(s)")
    for it in items[: args.limit]:
        title = it["metadata"]["conversation_title"]
        print(f"  {it['timestamp'][:10]} · {title[:50]} · turn {it['metadata']['turn_index']}")
