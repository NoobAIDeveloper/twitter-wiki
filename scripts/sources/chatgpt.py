#!/usr/bin/env python3
"""
ChatGPT source adapter.

Uses the same cookie-extraction stack as the X source to pull the logged-in
ChatGPT session cookie from the user's browser, exchanges it for a Bearer
access token via `/api/auth/session`, then paginates the user's conversation
list and extracts Q+A turn pairs as Items.

This is the "fragile but zero-friction" path. ChatGPT rotates endpoints and
sometimes adds Cloudflare challenges. When that happens, the export-zip
fallback (/kb-import-chatgpt) is the escape hatch.
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


SOURCE_ID = "chatgpt"
HOST_PATTERNS = ("%chatgpt.com", "%chat.openai.com")
COOKIE_NAMES = {"__Secure-next-auth.session-token"}
BASE = "https://chatgpt.com"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 personal-wiki"

# Hard caps so a single massive conversation can't dominate.
MAX_USER_LEN = 4000
MAX_ASSISTANT_LEN = 4000
MAX_CONVERSATIONS = 1000
PAGE_SIZE = 28


class ChatGPTAuthError(RuntimeError):
    pass


class ChatGPTRateLimitError(RuntimeError):
    pass


def _get_session_token(browser: str = "auto") -> str:
    cookies = extract_cookies(
        host_patterns=HOST_PATTERNS,
        wanted_names=COOKIE_NAMES,
        browser=browser,
        site_label="chatgpt.com",
    )
    token = cookies.get("__Secure-next-auth.session-token")
    if not token:
        raise ChatGPTAuthError(
            "Missing __Secure-next-auth.session-token cookie. "
            "Log into https://chatgpt.com in your browser and retry."
        )
    return token


def _request(url: str, cookies: dict[str, str], bearer: str | None = None) -> dict[str, Any]:
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Cookie": cookie_header,
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
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
            raise ChatGPTAuthError(
                f"ChatGPT returned {exc.code}. Your session cookie is stale — "
                f"log out and back into chatgpt.com and retry. Body: {body}"
            ) from exc
        if exc.code == 429:
            raise ChatGPTRateLimitError(
                f"ChatGPT rate-limited us ({exc.code}). Wait a few minutes and retry."
            ) from exc
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc


def _get_access_token(session_token: str) -> str:
    """Exchange the session cookie for a short-lived Bearer access token."""
    cookies = {"__Secure-next-auth.session-token": session_token}
    data = _request(f"{BASE}/api/auth/session", cookies)
    token = data.get("accessToken")
    if not token:
        raise ChatGPTAuthError(
            "Could not obtain accessToken from /api/auth/session — your session "
            "may have expired or ChatGPT's auth flow has changed."
        )
    return token


def _list_conversations(
    session_token: str,
    access_token: str,
    *,
    stop_at_update_time: str | None = None,
    hard_limit: int = MAX_CONVERSATIONS,
) -> list[dict[str, Any]]:
    cookies = {"__Secure-next-auth.session-token": session_token}
    conversations: list[dict[str, Any]] = []
    offset = 0
    while offset < hard_limit:
        url = f"{BASE}/backend-api/conversations?offset={offset}&limit={PAGE_SIZE}&order=updated"
        data = _request(url, cookies, bearer=access_token)
        items = data.get("items") or []
        if not items:
            break
        reached_known = False
        for conv in items:
            if stop_at_update_time and (conv.get("update_time") or "") <= stop_at_update_time:
                reached_known = True
                break
            conversations.append(conv)
        if reached_known:
            break
        if len(items) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.3)
    return conversations


def _fetch_conversation(conv_id: str, session_token: str, access_token: str) -> dict[str, Any]:
    cookies = {"__Secure-next-auth.session-token": session_token}
    return _request(f"{BASE}/backend-api/conversation/{conv_id}", cookies, bearer=access_token)


def _pair_turns(conv: dict[str, Any]) -> list[Item]:
    """Walk the conversation's message tree in linear order (by create_time)
    and emit one Item per user-prompt + assistant-reply chunk.
    """
    mapping = conv.get("mapping") or {}
    # Flatten into chronological order by create_time.
    messages: list[dict[str, Any]] = []
    for node in mapping.values():
        m = node.get("message")
        if not isinstance(m, dict):
            continue
        messages.append(m)
    messages.sort(key=lambda m: m.get("create_time") or 0)

    def _text_of(msg: dict[str, Any]) -> str:
        content = msg.get("content") or {}
        ctype = content.get("content_type")
        if ctype == "text":
            return "\n".join(content.get("parts") or [])
        if ctype == "multimodal_text":
            parts = []
            for p in content.get("parts") or []:
                if isinstance(p, str):
                    parts.append(p)
                elif isinstance(p, dict) and p.get("text"):
                    parts.append(p["text"])
            return "\n".join(parts)
        # code, tether_browsing_code, etc. — ignore
        return ""

    conv_id = conv.get("conversation_id") or conv.get("id") or ""
    title = conv.get("title") or "(untitled)"

    items: list[Item] = []
    turn_index = 0
    i = 0
    while i < len(messages):
        m = messages[i]
        author = (m.get("author") or {}).get("role")
        if author != "user":
            i += 1
            continue
        user_text = _text_of(m).strip()
        if not user_text:
            i += 1
            continue
        if len(user_text) > MAX_USER_LEN:
            user_text = user_text[:MAX_USER_LEN] + "\n[...truncated]"
        ts_epoch = m.get("create_time") or 0
        ts = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_epoch))
            if ts_epoch
            else ""
        )

        # Collect assistant replies until the next user message.
        assistant_parts: list[str] = []
        j = i + 1
        while j < len(messages):
            mj = messages[j]
            role = (mj.get("author") or {}).get("role")
            if role == "user":
                break
            if role == "assistant":
                t = _text_of(mj).strip()
                if t:
                    assistant_parts.append(t)
            j += 1

        assistant_text = "\n\n".join(assistant_parts).strip()
        if len(assistant_text) > MAX_ASSISTANT_LEN:
            assistant_text = assistant_text[:MAX_ASSISTANT_LEN] + "\n[...truncated]"

        if len(user_text) < 20 and len(assistant_text) < 100:
            i = j
            continue

        body = (
            f"**User:** {user_text}\n\n**ChatGPT:** {assistant_text}"
            if assistant_text
            else f"**User:** {user_text}"
        )
        items.append(
            Item(
                id=f"{SOURCE_ID}:{conv_id}:{turn_index}",
                source=SOURCE_ID,
                text=body,
                timestamp=ts,
                author=None,
                url=f"{BASE}/c/{conv_id}" if conv_id else None,
                engagement=None,
                media=[],
                metadata={
                    "conversation_id": conv_id,
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
    """Sync ChatGPT conversations for the logged-in user.

    Incremental by default: stops walking the conversation list when it hits
    one with update_time <= the last-seen value in chatgpt-sync-meta.json.
    Pass full=True to re-fetch everything.
    """
    state_dir = kb_dir / ".twitter-wiki"
    state_dir.mkdir(parents=True, exist_ok=True)
    meta_path = state_dir / "chatgpt-sync-meta.json"

    stop_at: str | None = None
    if not full and meta_path.exists():
        try:
            stop_at = json.loads(meta_path.read_text()).get("lastUpdateTime")
        except json.JSONDecodeError:
            stop_at = None

    session_token = _get_session_token(browser=browser)
    access_token = _get_access_token(session_token)
    print(f"[chatgpt] got access token", file=sys.stderr)

    conv_list = _list_conversations(session_token, access_token, stop_at_update_time=stop_at)
    print(f"[chatgpt] {len(conv_list)} new/updated conversation(s)", file=sys.stderr)

    # Oldest-first so we checkpoint newest_update_time monotonically — if we
    # get rate-limited midway, the cursor is safe to persist.
    conv_list = sorted(conv_list, key=lambda c: c.get("update_time") or "")

    all_items: list[Item] = []
    newest_update_time = stop_at or ""
    rate_limited = False
    for i, summary in enumerate(conv_list, 1):
        conv_id = summary.get("id")
        if not conv_id:
            continue
        try:
            conv = _fetch_conversation(conv_id, session_token, access_token)
        except ChatGPTAuthError:
            raise
        except ChatGPTRateLimitError:
            print(
                f"[chatgpt] rate-limited at {i-1}/{len(conv_list)}; "
                f"persisting progress and stopping.",
                file=sys.stderr,
            )
            rate_limited = True
            break
        except Exception as exc:  # noqa: BLE001
            print(f"[chatgpt] skip conversation {conv_id}: {exc}", file=sys.stderr)
            continue
        items_from_conv = _pair_turns(conv)
        all_items.extend(items_from_conv)
        ut = summary.get("update_time") or ""
        if ut > newest_update_time:
            newest_update_time = ut
        if i % 10 == 0:
            print(f"[chatgpt] fetched {i}/{len(conv_list)}", file=sys.stderr)
        time.sleep(1.2)

    if newest_update_time:
        meta_path.write_text(
            json.dumps({"lastUpdateTime": newest_update_time}, indent=2) + "\n"
        )

    if rate_limited:
        print(
            "[chatgpt] partial sync — rerun /kb-sync --source chatgpt in a few "
            "minutes to pick up the rest.",
            file=sys.stderr,
        )

    return [it.to_json() for it in all_items]


def ingest_export(zip_path: Path) -> list[dict[str, Any]]:
    """Parse an official ChatGPT export zip and emit Items.

    The export contains a `conversations.json` file whose entries have the
    same shape the API returns (title, conversation_id, mapping, ...), so
    we can reuse `_pair_turns` directly.
    """
    import zipfile

    if not zip_path.exists():
        raise FileNotFoundError(f"Export zip not found: {zip_path}")

    with zipfile.ZipFile(zip_path) as zf:
        try:
            raw = zf.read("conversations.json")
        except KeyError as exc:
            raise ValueError(
                f"{zip_path.name} does not contain conversations.json — is this "
                f"a ChatGPT export?"
            ) from exc

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

    ap = argparse.ArgumentParser(description="Sync ChatGPT conversations as Items.")
    ap.add_argument("--kb", type=Path, default=Path.cwd())
    ap.add_argument("--browser", default="auto")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    items = sync(args.kb, browser=args.browser, full=args.full)
    print(f"[chatgpt] {len(items)} Q+A pair(s)")
    for it in items[: args.limit]:
        title = it["metadata"]["conversation_title"]
        print(f"  {it['timestamp'][:10]} · {title[:50]} · turn {it['metadata']['turn_index']}")
