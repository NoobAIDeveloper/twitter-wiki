#!/usr/bin/env python3
"""
graphql.py — Fetch Twitter/X bookmarks via the internal GraphQL API.

Python port of the TypeScript reference implementation at
https://github.com/afar1/fieldtheory-cli/blob/main/src/graphql-bookmarks.ts

This calls the same `Bookmarks` GraphQL endpoint that the X.com web client
uses, paginates through all of the authenticated user's bookmarks, and yields
bookmark dicts that conform to the twitter-wiki canonical schema.

Usage:
    python3 scripts/graphql.py --ct0 <ct0> --auth-token <auth_token> [--max-pages N]

Only stdlib is used (urllib.request + json). No third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator


# ---------------------------------------------------------------------------
# Constants — copied verbatim from graphql-bookmarks.ts
# These are PUBLIC values that every X.com web user sends in their browser.
# Twitter rejects requests with mismatched feature flags, so do NOT "modernize".
# ---------------------------------------------------------------------------

X_PUBLIC_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

BOOKMARKS_QUERY_ID = "Z9GWmP0kP2dajyckAaDUBw"
BOOKMARKS_OPERATION = "Bookmarks"

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

GRAPHQL_FEATURES: dict[str, bool] = {
    "graphql_timeline_v2_bookmark_timeline": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_uc_gql_enabled": True,
    "vibe_api_enabled": True,
    "responsive_web_text_conversations_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "responsive_web_media_download_video_enabled": False,
}

DEFAULT_PAGE_COUNT = 20
TWITTER_EPOCH_MS = 1288834974657


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def snowflake_to_datetime(tweet_id: str) -> datetime:
    """Decode a tweet snowflake ID to its creation timestamp (UTC)."""
    return datetime.fromtimestamp(
        ((int(tweet_id) >> 22) + TWITTER_EPOCH_MS) / 1000,
        tz=timezone.utc,
    )


def _now_iso() -> str:
    """Return the current time as an ISO-8601 string with millisecond Z suffix."""
    # Match the TS "new Date().toISOString()" format, e.g. 2026-04-08T05:54:46.537Z
    return (
        datetime.now(tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{datetime.now(tz=timezone.utc).microsecond // 1000:03d}Z"
    )


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _build_url(cursor: str | None, count: int = DEFAULT_PAGE_COUNT) -> str:
    """Build the GraphQL request URL with variables and features in the query string."""
    variables: dict[str, Any] = {"count": count}
    if cursor:
        variables["cursor"] = cursor
    params = urllib.parse.urlencode(
        {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(GRAPHQL_FEATURES, separators=(",", ":")),
        }
    )
    return (
        f"https://x.com/i/api/graphql/"
        f"{BOOKMARKS_QUERY_ID}/{BOOKMARKS_OPERATION}?{params}"
    )


def _build_headers(ct0: str, auth_token: str) -> dict[str, str]:
    """Build the headers the X.com web client sends for authenticated GraphQL calls."""
    return {
        "authorization": f"Bearer {X_PUBLIC_BEARER}",
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "content-type": "application/json",
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://x.com",
        "referer": "https://x.com/i/bookmarks",
        "user-agent": CHROME_UA,
        "cookie": f"auth_token={auth_token}; ct0={ct0}",
    }


# ---------------------------------------------------------------------------
# HTTP with retry/backoff
# ---------------------------------------------------------------------------


class AuthError(RuntimeError):
    """Raised on 401/403 — cookies are stale and the user must re-login to X."""


class RateLimitError(RuntimeError):
    """Raised when we exhaust 429 retries."""


def _retry_after_seconds(err: urllib.error.HTTPError) -> float | None:
    """Parse a Retry-After header (either seconds or HTTP-date)."""
    header = err.headers.get("Retry-After") if err.headers else None
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(header)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(tz=timezone.utc)).total_seconds())
    except Exception:
        return None


def _fetch_page(url: str, headers: dict[str, str]) -> dict[str, Any]:
    """
    Perform one GraphQL request with retry/backoff.

    - 429: exponential backoff 15 -> 30 -> 60 -> 120 s (honors Retry-After).
    - 5xx: shorter backoff 5 -> 10 -> 20 s (max 3 retries).
    - 401/403: raise AuthError immediately.
    """
    last_error: Exception | None = None

    # 429 retry loop: up to 4 attempts.
    rate_limit_waits = [15, 30, 60, 120]
    server_error_waits = [5, 10, 20]

    rate_limit_attempt = 0
    server_error_attempt = 0

    while True:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as err:
            status = err.code

            if status in (401, 403):
                body_snippet = ""
                try:
                    body_snippet = err.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    pass
                raise AuthError(
                    f"Auth failed ({status}). Your X cookies are stale — "
                    f"re-login to X and copy fresh ct0 / auth_token values. "
                    f"Body: {body_snippet}"
                ) from err

            if status == 429:
                if rate_limit_attempt >= len(rate_limit_waits):
                    raise RateLimitError(
                        f"Rate limited (429) after "
                        f"{rate_limit_attempt} retries; giving up."
                    ) from err
                wait = _retry_after_seconds(err) or rate_limit_waits[rate_limit_attempt]
                _log(
                    f"[graphql] 429 rate limited — sleeping {wait:.0f}s "
                    f"(attempt {rate_limit_attempt + 1}/{len(rate_limit_waits)})"
                )
                time.sleep(wait)
                rate_limit_attempt += 1
                last_error = err
                continue

            if 500 <= status < 600:
                if server_error_attempt >= len(server_error_waits):
                    raise RuntimeError(
                        f"Server error ({status}) after "
                        f"{server_error_attempt} retries; giving up."
                    ) from err
                wait = server_error_waits[server_error_attempt]
                _log(
                    f"[graphql] {status} server error — sleeping {wait}s "
                    f"(attempt {server_error_attempt + 1}/{len(server_error_waits)})"
                )
                time.sleep(wait)
                server_error_attempt += 1
                last_error = err
                continue

            # Any other HTTP error: surface it.
            body_snippet = ""
            try:
                body_snippet = err.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise RuntimeError(
                f"GraphQL HTTP {status}: {err.reason}. Body: {body_snippet}"
            ) from err

        except urllib.error.URLError as err:
            # Transient network error — treat like a 5xx.
            if server_error_attempt >= len(server_error_waits):
                raise RuntimeError(
                    f"Network error after {server_error_attempt} retries: {err}"
                ) from err
            wait = server_error_waits[server_error_attempt]
            _log(
                f"[graphql] network error — sleeping {wait}s "
                f"(attempt {server_error_attempt + 1}/{len(server_error_waits)}): {err}"
            )
            time.sleep(wait)
            server_error_attempt += 1
            last_error = err
            continue


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _find_entries_and_cursor(payload: dict[str, Any]) -> tuple[list[dict], str | None]:
    """
    Walk the GraphQL response and return (tweet_entries, next_cursor).

    Entries come from the `TimelineAddEntries` instruction under
    data.bookmark_timeline_v2.timeline.instructions[].
    Cursor is the `content.value` of the `cursor-bottom-*` entry.
    """
    timeline = (
        payload.get("data", {})
        .get("bookmark_timeline_v2", {})
        .get("timeline", {})
    )
    instructions = timeline.get("instructions") or []
    entries: list[dict] = []
    for instr in instructions:
        itype = instr.get("type")
        if itype == "TimelineAddEntries":
            entries.extend(instr.get("entries") or [])
        elif itype == "TimelineAddToModule":
            # Rare, but some bookmark responses include module wrappers.
            module_items = instr.get("moduleItems") or []
            entries.extend(module_items)

    tweet_entries: list[dict] = []
    next_cursor: str | None = None

    for entry in entries:
        entry_id = entry.get("entryId", "") or ""
        if entry_id.startswith("cursor-bottom"):
            next_cursor = (entry.get("content") or {}).get("value")
            continue
        if entry_id.startswith("cursor-top"):
            continue
        # Real tweet entries — the reference impl identifies them by the
        # presence of content.itemContent.tweet_results.result rather than by
        # entryId prefix (though "tweet-" is the common prefix).
        content = entry.get("content") or {}
        item_content = content.get("itemContent") or {}
        if item_content.get("tweet_results"):
            tweet_entries.append(entry)

    return tweet_entries, next_cursor


def _unwrap_tweet(tweet_result: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a TweetWithVisibilityResults envelope if present."""
    return tweet_result.get("tweet") or tweet_result


def _get(obj: Any, *path: str, default: Any = None) -> Any:
    """Safely navigate nested dicts; returns default on any missing key/None."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def convert_tweet_to_record(
    tweet_result: dict[str, Any],
    now: str,
) -> dict[str, Any] | None:
    """
    Convert a raw GraphQL tweet result into a canonical bookmark dict.

    Returns None if the entry is missing critical fields (legacy/id).
    Missing fields are defaulted to empty string / empty list / 0 / False
    so the output always conforms to the twitter-wiki schema.
    """
    tweet = _unwrap_tweet(tweet_result)
    legacy = _get(tweet, "legacy")
    if not isinstance(legacy, dict):
        return None

    tweet_id = legacy.get("id_str") or tweet.get("rest_id")
    if not tweet_id:
        return None
    tweet_id = str(tweet_id)

    # ---- author ----------------------------------------------------------
    user_result = _get(tweet, "core", "user_results", "result") or {}
    author_handle = (
        _get(user_result, "core", "screen_name")
        or _get(user_result, "legacy", "screen_name")
        or ""
    )
    author_name = (
        _get(user_result, "core", "name")
        or _get(user_result, "legacy", "name")
        or ""
    )
    author_profile_image_url = (
        _get(user_result, "avatar", "image_url")
        or _get(user_result, "legacy", "profile_image_url_https")
        or _get(user_result, "legacy", "profile_image_url")
        or ""
    )

    bio = _get(user_result, "legacy", "description") or ""
    follower_count = _get(user_result, "legacy", "followers_count") or 0
    following_count = _get(user_result, "legacy", "friends_count") or 0
    is_verified = bool(
        user_result.get("is_blue_verified")
        if isinstance(user_result, dict)
        else False
    ) or bool(_get(user_result, "legacy", "verified"))

    loc_raw = user_result.get("location") if isinstance(user_result, dict) else None
    if isinstance(loc_raw, dict):
        location = loc_raw.get("location") or ""
    else:
        location = _get(user_result, "legacy", "location") or ""

    author = {
        "id": str(user_result.get("rest_id") or "") if isinstance(user_result, dict) else "",
        "handle": author_handle,
        "name": author_name,
        "profileImageUrl": author_profile_image_url,
        "bio": bio,
        "followerCount": int(follower_count or 0),
        "followingCount": int(following_count or 0),
        "isVerified": is_verified,
        "location": location,
        "snapshotAt": now,
    }

    # ---- media -----------------------------------------------------------
    media_entities: list[dict] = (
        _get(legacy, "extended_entities", "media")
        or _get(legacy, "entities", "media")
        or []
    )
    media_urls: list[str] = []
    media_objects: list[dict] = []
    for m in media_entities:
        if not isinstance(m, dict):
            continue
        url = m.get("media_url_https") or m.get("media_url")
        if url:
            media_urls.append(url)
        video_variants: list[dict] = []
        variants = _get(m, "video_info", "variants") or []
        if isinstance(variants, list):
            for v in variants:
                if isinstance(v, dict) and v.get("content_type") == "video/mp4":
                    video_variants.append(
                        {"bitrate": v.get("bitrate"), "url": v.get("url")}
                    )
        media_objects.append(
            {
                "type": m.get("type") or "",
                "url": url or "",
                "expandedUrl": m.get("expanded_url") or "",
                "width": _get(m, "original_info", "width") or 0,
                "height": _get(m, "original_info", "height") or 0,
                "altText": m.get("ext_alt_text") or "",
                "videoVariants": video_variants,
            }
        )

    # ---- links -----------------------------------------------------------
    url_entities = _get(legacy, "entities", "urls") or []
    links: list[str] = []
    for u in url_entities:
        if not isinstance(u, dict):
            continue
        expanded = u.get("expanded_url")
        if expanded and "t.co" not in expanded:
            links.append(expanded)

    # ---- quoted tweet ----------------------------------------------------
    quoted_tweet: dict[str, Any] | None = None
    quoted_result = _get(tweet, "quoted_status_result", "result")
    if isinstance(quoted_result, dict):
        qt = _unwrap_tweet(quoted_result)
        qt_legacy = _get(qt, "legacy")
        if isinstance(qt_legacy, dict):
            qt_id = qt_legacy.get("id_str") or qt.get("rest_id") or ""
            qt_user = _get(qt, "core", "user_results", "result") or {}
            qt_handle = (
                _get(qt_user, "core", "screen_name")
                or _get(qt_user, "legacy", "screen_name")
                or "_"
            )
            qt_media_entities = (
                _get(qt_legacy, "extended_entities", "media")
                or _get(qt_legacy, "entities", "media")
                or []
            )
            qt_media_urls = [
                m.get("media_url_https") or m.get("media_url")
                for m in qt_media_entities
                if isinstance(m, dict) and (m.get("media_url_https") or m.get("media_url"))
            ]
            qt_media_objects = [
                {
                    "type": m.get("type") or "",
                    "url": m.get("media_url_https") or m.get("media_url") or "",
                    "expandedUrl": m.get("expanded_url") or "",
                    "width": _get(m, "original_info", "width") or 0,
                    "height": _get(m, "original_info", "height") or 0,
                }
                for m in qt_media_entities
                if isinstance(m, dict)
            ]
            quoted_tweet = {
                "id": str(qt_id),
                "text": qt_legacy.get("full_text") or qt_legacy.get("text") or "",
                "authorHandle": qt_handle,
                "authorName": _get(qt_user, "core", "name")
                or _get(qt_user, "legacy", "name")
                or "",
                "authorProfileImageUrl": _get(qt_user, "avatar", "image_url")
                or _get(qt_user, "legacy", "profile_image_url_https")
                or "",
                "postedAt": qt_legacy.get("created_at") or "",
                "media": qt_media_urls,
                "mediaObjects": qt_media_objects,
                "url": f"https://x.com/{qt_handle}/status/{qt_id}",
            }

    # ---- text (note tweets win over legacy.full_text) --------------------
    note_text = _get(tweet, "note_tweet", "note_tweet_results", "result", "text")
    text = note_text or legacy.get("full_text") or legacy.get("text") or ""

    # ---- engagement ------------------------------------------------------
    view_count_raw = _get(tweet, "views", "count")
    try:
        view_count = int(view_count_raw) if view_count_raw is not None else 0
    except (TypeError, ValueError):
        view_count = 0

    engagement = {
        "likeCount": int(legacy.get("favorite_count") or 0),
        "repostCount": int(legacy.get("retweet_count") or 0),
        "replyCount": int(legacy.get("reply_count") or 0),
        "quoteCount": int(legacy.get("quote_count") or 0),
        "bookmarkCount": int(legacy.get("bookmark_count") or 0),
        "viewCount": view_count,
    }

    # The GraphQL response does NOT expose a per-bookmark bookmarkedAt.
    # The TS impl sets it to null and relies on a sanitizer elsewhere.
    # Default to now (sync time) — callers can overwrite with a better value
    # (e.g. the previous known value) during incremental merge.
    bookmarked_at = now

    record: dict[str, Any] = {
        "id": tweet_id,
        "tweetId": tweet_id,
        "url": f"https://x.com/{author_handle or '_'}/status/{tweet_id}",
        "text": text,
        "authorHandle": author_handle,
        "authorName": author_name,
        "authorProfileImageUrl": author_profile_image_url,
        "author": author,
        "postedAt": legacy.get("created_at") or "",
        "bookmarkedAt": bookmarked_at,
        "syncedAt": now,
        "conversationId": legacy.get("conversation_id_str") or "",
        "inReplyToStatusId": legacy.get("in_reply_to_status_id_str") or "",
        "inReplyToUserId": legacy.get("in_reply_to_user_id_str") or "",
        "quotedStatusId": legacy.get("quoted_status_id_str") or "",
        "quotedTweet": quoted_tweet,
        "language": legacy.get("lang") or "",
        "sourceApp": legacy.get("source") or "",
        "possiblySensitive": bool(legacy.get("possibly_sensitive") or False),
        "engagement": engagement,
        "media": media_urls,
        "mediaObjects": media_objects,
        "links": links,
        "tags": [],
        "ingestedVia": "graphql",
    }
    return record


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class FetchOptions:
    """Options for fetch_bookmarks()."""

    ct0: str
    auth_token: str
    cursor: str | None = None
    max_pages: int = 200
    page_delay_ms: int = 600
    stop_at_id: str | None = None  # stop when this tweet id is encountered
    on_page: Callable[[list[dict], dict], None] | None = None  # progress callback


def fetch_bookmarks(opts: FetchOptions) -> Iterator[dict]:
    """
    Yield bookmark dicts (matching the twitter-wiki canonical schema)
    page by page, using X's internal `Bookmarks` GraphQL endpoint.

    Handles:
      * cursor-based pagination (stops when cursor is missing/empty)
      * max_pages limit
      * stop_at_id for incremental sync
      * 429 exponential backoff (honors Retry-After)
      * 5xx shorter backoff
      * 401/403 -> AuthError

    The `on_page` callback, if provided, is invoked once per page with
    (records_on_this_page, meta_dict) where meta_dict contains
    {page, cursor, total}.
    """
    if not opts.ct0 or not opts.auth_token:
        raise ValueError("ct0 and auth_token are required")

    headers = _build_headers(opts.ct0, opts.auth_token)
    cursor = opts.cursor
    page_delay_sec = max(0.0, opts.page_delay_ms / 1000.0)
    total = 0

    for page in range(1, opts.max_pages + 1):
        url = _build_url(cursor, count=DEFAULT_PAGE_COUNT)
        _log(f"[graphql] page {page} cursor={cursor!s:.60}")

        payload = _fetch_page(url, headers)
        entries, next_cursor = _find_entries_and_cursor(payload)

        now = _now_iso()
        page_records: list[dict] = []
        stop_signal = False

        for entry in entries:
            tweet_result = _get(entry, "content", "itemContent", "tweet_results", "result")
            if not isinstance(tweet_result, dict):
                continue
            record = convert_tweet_to_record(tweet_result, now)
            if record is None:
                continue
            if opts.stop_at_id and record["tweetId"] == opts.stop_at_id:
                _log(f"[graphql] hit stop_at_id={opts.stop_at_id}; stopping")
                stop_signal = True
                break
            page_records.append(record)

        total += len(page_records)

        if opts.on_page is not None:
            try:
                opts.on_page(
                    page_records,
                    {"page": page, "cursor": cursor, "total": total},
                )
            except Exception as cb_err:  # noqa: BLE001
                _log(f"[graphql] on_page callback raised: {cb_err}")

        for rec in page_records:
            yield rec

        if stop_signal:
            return

        # End of timeline: no tweets this page AND no advancing cursor.
        if not page_records and (not next_cursor or next_cursor == cursor):
            _log("[graphql] empty page and no new cursor; done")
            return

        if not next_cursor:
            _log("[graphql] no cursor-bottom in response; done")
            return

        if next_cursor == cursor:
            _log("[graphql] cursor did not advance; done")
            return

        cursor = next_cursor

        if page < opts.max_pages and page_delay_sec > 0:
            time.sleep(page_delay_sec)

    _log(f"[graphql] reached max_pages={opts.max_pages}; stopping")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Twitter/X bookmarks via GraphQL and print as JSONL."
    )
    parser.add_argument("--ct0", required=True, help="ct0 cookie value (csrf token)")
    parser.add_argument(
        "--auth-token", required=True, help="auth_token cookie value"
    )
    parser.add_argument("--cursor", default=None, help="Start cursor (optional)")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument(
        "--page-delay-ms",
        type=int,
        default=600,
        help="Delay between pages in milliseconds",
    )
    parser.add_argument(
        "--stop-at-id",
        default=None,
        help="Stop when this tweet id is encountered (incremental sync)",
    )
    args = parser.parse_args(argv)

    opts = FetchOptions(
        ct0=args.ct0,
        auth_token=args.auth_token,
        cursor=args.cursor,
        max_pages=args.max_pages,
        page_delay_ms=args.page_delay_ms,
        stop_at_id=args.stop_at_id,
    )

    count = 0
    try:
        for record in fetch_bookmarks(opts):
            print(json.dumps(record, ensure_ascii=False))
            count += 1
    except AuthError as err:
        _log(f"[graphql] AUTH ERROR: {err}")
        return 2
    except RateLimitError as err:
        _log(f"[graphql] RATE LIMITED: {err}")
        return 3
    except KeyboardInterrupt:
        _log("[graphql] interrupted")
        return 130

    _log(f"[graphql] fetched {count} bookmarks")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
