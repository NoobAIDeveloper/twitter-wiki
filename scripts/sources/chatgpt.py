#!/usr/bin/env python3
"""
ChatGPT source adapter.

Pulls the logged-in ChatGPT session cookie from the user's browser (plus any
Cloudflare cookies it's collected), exchanges it for a Bearer access token
via `/api/auth/session`, then paginates the user's conversation list and
extracts Q+A turn pairs as Items.

Fragile path: ChatGPT sits behind Cloudflare bot mitigation. We pass along
`cf_clearance` when present and send Chromium-shaped headers to avoid the
challenge page. When CF blocks us anyway, `/kb-request-chatgpt-export` +
`/kb-import-chatgpt` is the escape hatch.
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


SOURCE_ID = "chatgpt"
HOST_PATTERNS = ("%chatgpt.com", "%chat.openai.com")
SESSION_COOKIE = "__Secure-next-auth.session-token"
COOKIE_NAMES = {SESSION_COOKIE}
# OpenAI sets these during login / sensitive-op reauth. We pass whatever
# the browser currently holds — they're rotated server-side and signal
# "this session recently proved identity", which the export endpoint checks.
OAI_SESSION_COOKIES = {
    "__Secure-next-auth.callback-url",
    "_puid",
    "oai-did",
    "oai-hlib",
    "oai-hm",
    "oai-sc",
}
BASE = "https://chatgpt.com"

# Hard caps so a single massive conversation can't dominate.
MAX_USER_LEN = 4000
MAX_ASSISTANT_LEN = 4000
MAX_CONVERSATIONS = 1000
PAGE_SIZE = 28


class ChatGPTAuthError(RuntimeError):
    pass


class ChatGPTRateLimitError(RuntimeError):
    def __init__(self, message: str, retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ChatGPTBlockedError(RuntimeError):
    """Cloudflare is serving a challenge page instead of the real API.
    Not a cookie problem — fixes are orthogonal (wait, revisit in browser,
    or use the export zip)."""
    pass


def _get_cookies(browser: str = "auto") -> dict[str, str]:
    """Pull the session cookie plus any Cloudflare cookies the browser
    holds. `cf_clearance` is the key CF signal when present; we still
    work without it but get challenged more readily."""
    cookies = extract_cookies(
        host_patterns=HOST_PATTERNS,
        wanted_names=COOKIE_NAMES,
        optional_names=CLOUDFLARE_OPTIONAL_COOKIES | OAI_SESSION_COOKIES,
        browser=browser,
        site_label="chatgpt.com",
    )
    if SESSION_COOKIE not in cookies:
        raise ChatGPTAuthError(
            f"Missing {SESSION_COOKIE} cookie. "
            "Log into https://chatgpt.com in your browser and retry."
        )
    return cookies


def _request(
    url: str,
    cookies: dict[str, str],
    bearer: str | None = None,
    *,
    method: str = "GET",
    body: bytes | None = None,
) -> dict[str, Any]:
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    extra = {"Cookie": cookie_header}
    if bearer:
        extra["Authorization"] = f"Bearer {bearer}"
    if body is not None:
        extra["Content-Type"] = "application/json"
    headers = browser_headers(BASE, extra=extra)
    req = urllib.request.Request(url, headers=headers, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        if exc.code == 403 and looks_like_cf_block(raw):
            raise ChatGPTBlockedError(
                "ChatGPT is behind a Cloudflare challenge. Open "
                "https://chatgpt.com in the same browser (which refreshes "
                "cf_clearance), wait 30+ minutes if the block persists, or "
                "run /kb-request-chatgpt-export to use the export-zip fallback."
            ) from exc
        snippet = raw[:300]
        if exc.code in (401, 403):
            if "reauth_required" in raw or "Recent login required" in raw:
                raise ChatGPTAuthError(
                    "ChatGPT requires a recent login for this action (OpenAI "
                    "gates sensitive endpoints like data export behind fresh "
                    "auth). Visit chatgpt.com, re-enter your password, then "
                    "retry. Or go to Settings → Data Controls → Export data "
                    "in the browser — same endpoint, authenticated UI path."
                ) from exc
            raise ChatGPTAuthError(
                f"ChatGPT returned {exc.code}. Your session cookie is stale — "
                f"log out and back into chatgpt.com and retry. Body: {snippet}"
            ) from exc
        if exc.code == 429:
            try:
                retry_after = int(exc.headers.get("Retry-After", "60"))
            except (TypeError, ValueError):
                retry_after = 60
            retry_after = max(10, min(retry_after, 300))
            raise ChatGPTRateLimitError(
                f"ChatGPT rate-limited us; Retry-After={retry_after}s.",
                retry_after=retry_after,
            ) from exc
        raise RuntimeError(f"HTTP {exc.code} from {url}: {snippet}") from exc


def _get_access_token(cookies: dict[str, str]) -> str:
    """Exchange the session cookie for a short-lived Bearer access token."""
    data = _request(f"{BASE}/api/auth/session", cookies)
    token = data.get("accessToken")
    if not token:
        raise ChatGPTAuthError(
            "Could not obtain accessToken from /api/auth/session — your session "
            "may have expired or ChatGPT's auth flow has changed."
        )
    return token


def _list_conversations(
    cookies: dict[str, str],
    access_token: str,
    *,
    stop_at_update_time: str | None = None,
    hard_limit: int = MAX_CONVERSATIONS,
) -> list[dict[str, Any]]:
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


def _fetch_conversation(
    conv_id: str, cookies: dict[str, str], access_token: str
) -> dict[str, Any]:
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

    cookies = _get_cookies(browser=browser)
    access_token = _get_access_token(cookies)
    print("[chatgpt] got access token", file=sys.stderr)

    conv_list = _list_conversations(cookies, access_token, stop_at_update_time=stop_at)
    # Average 5.5s/conv between our jitter (4-7s) + occasional 60s rate-limit
    # pauses. Communicate ETA up-front so the user knows to expect minutes,
    # not seconds, on the first full sync.
    eta_min = max(1, round(len(conv_list) * 5.5 / 60))
    print(
        f"[chatgpt] {len(conv_list)} new/updated conversation(s); "
        f"ETA ~{eta_min} min (rate-limited retries are automatic)",
        file=sys.stderr,
    )

    # Oldest-first so we checkpoint newest_update_time monotonically — if we
    # get interrupted midway, the cursor is safe to persist.
    conv_list = sorted(conv_list, key=lambda c: c.get("update_time") or "")

    all_items: list[Item] = []
    newest_update_time = stop_at or ""
    halt_msg: str | None = None
    for i, summary in enumerate(conv_list, 1):
        conv_id = summary.get("id")
        if not conv_id:
            continue
        # Retry with backoff on 429 — Retry-After tells us how long to wait.
        # Auth/CF errors propagate or halt; unknown errors skip one conv.
        conv: dict[str, Any] | None = None
        skip = False
        for attempt in range(3):
            try:
                conv = _fetch_conversation(conv_id, cookies, access_token)
                break
            except ChatGPTAuthError:
                raise
            except ChatGPTBlockedError:
                halt_msg = (
                    f"Cloudflare block at {i - 1}/{len(conv_list)}; progress saved. "
                    f"Revisit chatgpt.com in your browser or run "
                    f"/kb-request-chatgpt-export."
                )
                break
            except ChatGPTRateLimitError as exc:
                wait = exc.retry_after + random.uniform(0, 5)
                print(
                    f"[chatgpt] rate-limited at {i}/{len(conv_list)}; sleeping "
                    f"{wait:.0f}s (attempt {attempt + 1}/3)",
                    file=sys.stderr,
                )
                time.sleep(wait)
            except Exception as exc:  # noqa: BLE001
                print(f"[chatgpt] skip conversation {conv_id}: {exc}", file=sys.stderr)
                skip = True
                break
        if halt_msg:
            break
        if skip:
            continue
        if conv is None:
            halt_msg = (
                f"still rate-limited after 3 retries at {i - 1}/{len(conv_list)}; "
                f"progress saved. Rerun /kb-sync --source chatgpt in ~30 min to "
                f"pick up the remaining {len(conv_list) - i + 1}."
            )
            break
        all_items.extend(_pair_turns(conv))
        ut = summary.get("update_time") or ""
        if ut > newest_update_time:
            newest_update_time = ut
        if i % 10 == 0:
            print(f"[chatgpt] fetched {i}/{len(conv_list)}", file=sys.stderr)
        # 4-7s jitter: OpenAI's per-user limit bites around ~20 req/min, so
        # target ~11 req/min to stay comfortably below it.
        time.sleep(random.uniform(4.0, 7.0))

    if newest_update_time:
        meta_path.write_text(
            json.dumps({"lastUpdateTime": newest_update_time}, indent=2) + "\n"
        )

    if halt_msg:
        print(f"[chatgpt] {halt_msg}", file=sys.stderr)

    return [it.to_json() for it in all_items]


def _post_data_export(cookies: dict[str, str], access_token: str) -> None:
    _request(
        f"{BASE}/backend-api/accounts/data_export",
        cookies,
        bearer=access_token,
        method="POST",
        body=b"{}",
    )


def request_export_email(browser: str = "auto") -> str:
    """Trigger OpenAI's official data-export flow by POSTing to
    `/backend-api/accounts/data_export`. OpenAI emails the user a download
    link. Returns a human-readable status string for the slash command.

    OpenAI gates this endpoint behind a recent-login check. On the first 401
    with `reauth_required`, we open the Data Controls page in the user's
    browser, wait for them to re-authenticate, and retry the POST with a
    freshly-exchanged access token (reauth can rotate it).
    """
    import webbrowser

    cookies = _get_cookies(browser=browser)
    access_token = _get_access_token(cookies)
    try:
        _post_data_export(cookies, access_token)
    except ChatGPTAuthError as exc:
        if "recent login" not in str(exc):
            raise
        reauth_url = "https://chatgpt.com/#settings/DataControls"
        print(
            "[chatgpt] OpenAI needs a fresh login for data export.",
            file=sys.stderr,
        )
        opened = False
        try:
            opened = webbrowser.open(reauth_url)
        except Exception:  # noqa: BLE001
            opened = False
        if opened:
            print(f"[chatgpt] opened {reauth_url} in your default browser.", file=sys.stderr)
        else:
            print(
                f"[chatgpt] could not open a browser; visit this URL manually: "
                f"{reauth_url}",
                file=sys.stderr,
            )
        if not sys.stdin.isatty():
            raise ChatGPTAuthError(
                "Browser opened at chatgpt.com → Data Controls. Re-enter your "
                "password there (OpenAI will prompt you), then re-run this "
                "command. Non-interactive session, so we can't auto-retry."
            ) from exc
        print(
            "[chatgpt] Re-enter your password in the browser tab, then press "
            "Enter here to retry the export request...",
            file=sys.stderr,
            flush=True,
        )
        try:
            input()
        except EOFError as eof_exc:
            raise ChatGPTAuthError(
                "No TTY available to wait for re-auth. Re-authenticate in the "
                "browser tab that was opened, then re-run this command."
            ) from eof_exc
        # Reauth can rotate the session/access token — refresh both before retry.
        cookies = _get_cookies(browser=browser)
        access_token = _get_access_token(cookies)
        _post_data_export(cookies, access_token)
    return (
        "Export requested. OpenAI will email you a download link (usually "
        "within a few minutes, sometimes hours). When it arrives, download "
        "the zip and run `/kb-import-chatgpt <path-to-zip>`."
    )


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
    ap.add_argument(
        "--request-export",
        action="store_true",
        help="Trigger an export email (export-zip fallback) instead of syncing live.",
    )
    args = ap.parse_args()

    if args.request_export:
        print(request_export_email(browser=args.browser))
        raise SystemExit(0)

    items = sync(args.kb, browser=args.browser, full=args.full)
    print(f"[chatgpt] {len(items)} Q+A pair(s)")
    for it in items[: args.limit]:
        title = it["metadata"]["conversation_title"]
        print(f"  {it['timestamp'][:10]} · {title[:50]} · turn {it['metadata']['turn_index']}")
