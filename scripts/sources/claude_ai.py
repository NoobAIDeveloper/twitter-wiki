#!/usr/bin/env python3
"""
Claude.ai source adapter.

Same pattern as the ChatGPT adapter: pulls the logged-in `sessionKey`
cookie from the user's browser, lists conversations from the user's
organization(s), fetches per-conversation message trees, and emits
one Item per user+assistant Q+A turn pair.

Fragile (private API, subject to change). If it breaks, fall back to
/kb-import-claude with an official export zip.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cookies import extract_cookies  # noqa: E402

from .base import Item


SOURCE_ID = "claude-ai"
HOST_PATTERNS = ("%claude.ai",)
COOKIE_NAMES = {"sessionKey"}
BASE = "https://claude.ai"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 personal-wiki"

MAX_USER_LEN = 4000
MAX_ASSISTANT_LEN = 4000
PAGE_SIZE = 30
MAX_CONVERSATIONS = 1000


class ClaudeAIAuthError(RuntimeError):
    pass


class ClaudeAIRateLimitError(RuntimeError):
    pass


def _get_session_key(browser: str = "auto") -> str:
    cookies = extract_cookies(
        host_patterns=HOST_PATTERNS,
        wanted_names=COOKIE_NAMES,
        browser=browser,
        site_label="claude.ai",
    )
    key = cookies.get("sessionKey")
    if not key:
        raise ClaudeAIAuthError(
            "Missing sessionKey cookie. Log into https://claude.ai in your "
            "browser and retry."
        )
    return key


def _request(url: str, session_key: str) -> Any:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Cookie": f"sessionKey={session_key}",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        if exc.code in (401, 403):
            raise ClaudeAIAuthError(
                f"Claude.ai returned {exc.code}. Your session cookie is stale — "
                f"log out and back into claude.ai and retry. Body: {body}"
            ) from exc
        if exc.code == 429:
            raise ClaudeAIRateLimitError(
                f"Claude.ai rate-limited us ({exc.code}). Wait a few minutes."
            ) from exc
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc


def _list_organizations(session_key: str) -> list[str]:
    data = _request(f"{BASE}/api/organizations", session_key)
    if not isinstance(data, list):
        raise ClaudeAIAuthError(
            "Unexpected response from /api/organizations — session may be invalid."
        )
    org_ids = [o.get("uuid") for o in data if o.get("uuid")]
    if not org_ids:
        raise ClaudeAIAuthError("No organizations found on this Claude.ai account.")
    return org_ids


def _list_conversations(
    session_key: str,
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
        data = _request(url, session_key)
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


def _fetch_conversation(session_key: str, org_id: str, conv_uuid: str) -> dict[str, Any]:
    url = (
        f"{BASE}/api/organizations/{org_id}/chat_conversations/{conv_uuid}"
        f"?tree=True&rendering_mode=messages"
    )
    return _request(url, session_key)


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
    # Claude.ai chat_messages are chronological already, but sort by
    # created_at defensively.
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

    session_key = _get_session_key(browser=browser)
    print("[claude-ai] session key ok", file=sys.stderr)

    org_ids = _list_organizations(session_key)
    print(f"[claude-ai] {len(org_ids)} organization(s)", file=sys.stderr)

    all_conv_summaries: list[tuple[str, dict[str, Any]]] = []
    for org_id in org_ids:
        conv_list = _list_conversations(session_key, org_id, stop_at_update_time=stop_at)
        for c in conv_list:
            all_conv_summaries.append((org_id, c))

    print(
        f"[claude-ai] {len(all_conv_summaries)} new/updated conversation(s)",
        file=sys.stderr,
    )

    # Oldest first so the cursor advances monotonically on partial progress.
    all_conv_summaries.sort(key=lambda pair: pair[1].get("updated_at") or "")

    all_items: list[Item] = []
    newest_update_time = stop_at or ""
    rate_limited = False
    for i, (org_id, summary) in enumerate(all_conv_summaries, 1):
        conv_uuid = summary.get("uuid")
        if not conv_uuid:
            continue
        try:
            conv = _fetch_conversation(session_key, org_id, conv_uuid)
        except ClaudeAIAuthError:
            raise
        except ClaudeAIRateLimitError:
            print(
                f"[claude-ai] rate-limited at {i-1}/{len(all_conv_summaries)}; "
                f"persisting progress and stopping.",
                file=sys.stderr,
            )
            rate_limited = True
            break
        except Exception as exc:  # noqa: BLE001
            print(f"[claude-ai] skip conversation {conv_uuid}: {exc}", file=sys.stderr)
            continue
        all_items.extend(_pair_turns(conv))
        ut = summary.get("updated_at") or ""
        if ut > newest_update_time:
            newest_update_time = ut
        if i % 10 == 0:
            print(f"[claude-ai] fetched {i}/{len(all_conv_summaries)}", file=sys.stderr)
        time.sleep(1.2)

    if newest_update_time:
        meta_path.write_text(
            json.dumps({"lastUpdateTime": newest_update_time}, indent=2) + "\n"
        )

    if rate_limited:
        print(
            "[claude-ai] partial sync — rerun /kb-sync --source claude-ai in a "
            "few minutes to pick up the rest.",
            file=sys.stderr,
        )

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
