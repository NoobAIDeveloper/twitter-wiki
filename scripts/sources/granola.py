#!/usr/bin/env python3
"""
Granola source adapter.

Granola is a macOS-only meeting notetaker. Its desktop app stores every
meeting in a single local JSON cache at
`~/Library/Application Support/Granola/cache-v6.json` (older builds used
v3/v5 — `_default_cache_path` resolves whatever's newest). The cache is
the primary source for note + transcript content.

The cache is wrapped as `{"cache": "<json-string>"}` (pre-v6,
double-encoded) or `{"cache": {...}}` (v6+, direct). Either way, the
inner object has a `state` key with:

- `documents`: map of doc_id → meeting record (title, notes, dates, …)
- `meetingsMetadata`: map of doc_id → extra metadata (attendees, calendar)
- `transcripts`: map of doc_id → list of transcript segments
- `documentPanels`: map of doc_id → { panel_id → { original_content: html,
   content: prosemirror } } — this used to hold the AI-generated summary
   but Granola moved enhanced summaries server-side. The local panel map
   is parsed for forward-compat as a *fallback only*; in practice it's
   essentially always empty. The actual AI-summary HTML now lives behind
   `https://api.granola.ai/v1/get-document-panels` (see API path below).
- `documentLists` / `documentListsMetadata`: folder structure

API path (Granola's internal HTTP API). When `granola.use_api` is true
(default), the adapter pulls AI-enhanced summaries from
`https://api.granola.ai/v1/get-document-panels` for each meeting. The
WorkOS access token is auto-detected from
`~/Library/Application Support/Granola/supabase.json` (the desktop app's
session store) — no manual paste required. Free-tier users typically
get 401/403 on the panels endpoint; the adapter logs once and falls
back to local-only behavior for the rest of the run. Meetings without
an AI-enhanced summary return an empty panel list and fall back to the
local notes ProseMirror. The token can be overridden via
`granola.api_token` if a user wants to use a different account.

Chunking strategy — driven by `granola.content_mode` in
`.engram/sources.json`. Four modes:

- `notes`       — AI-summary if available (preferred), else user-typed
                  notes (ProseMirror headings).
- `transcript`  — only the raw transcript (size-chunked at 4000 chars).
- `both`        — notes/AI-summary first (heading-chunked), transcript
                  appended as additional chunks. DEFAULT when the
                  config field is missing or unrecognized.
- `auto`        — notes/AI-summary if its rendered body is substantive
                  (>200 chars excluding heading-only blocks), else
                  transcript, else both as a last resort.

In every non-transcript mode, an API-fetched AI summary takes priority
over the local-cache notes. Meetings with no usable content in any
selected stream are skipped.

Item id: `granola:<meeting_id>:<chunk_index>` — single id family
regardless of mode. In `both` mode notes/summary chunks come first
(indices 0..K), transcript chunks follow (indices K+1..N).

Config (optional) — all keys live under `granola` in
`.engram/sources.json`:

    {"granola": {"cache_path": "/custom/path/cache-v6.json",
                 "content_mode": "both",
                 "use_api": true,
                 "api_token": "..."}}
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .base import (
    Item,
    chunk_by_headings,
    chunk_by_size,
    drop_items_by_id_prefix,
    load_items,
)


SOURCE_ID = "granola"
GRANOLA_DIR = Path.home() / "Library" / "Application Support" / "Granola"
# Granola has shipped multiple cache versions (v3 pre-2025, v5, v6+). Prefer
# the newest cache-v*.json present so we don't break when they bump again.
_CACHE_GLOB = "cache-v*.json"


def _default_cache_path() -> Path:
    candidates = sorted(
        GRANOLA_DIR.glob(_CACHE_GLOB),
        key=lambda p: int(re.search(r"cache-v(\d+)\.json$", p.name).group(1))
        if re.search(r"cache-v(\d+)\.json$", p.name) else 0,
        reverse=True,
    )
    return candidates[0] if candidates else GRANOLA_DIR / "cache-v6.json"


# Back-compat: kept as an attribute many callers may reference.
DEFAULT_CACHE_PATH = _default_cache_path()


# ---- config / state --------------------------------------------------------

VALID_CONTENT_MODES = ("notes", "transcript", "both", "auto")
DEFAULT_CONTENT_MODE = "both"
# Auto heuristic: "notes is substantive" if its non-heading body chars
# across all heading-chunks exceeds this threshold.
AUTO_NOTES_BODY_THRESHOLD = 200


def _read_granola_config(kb_dir: Path) -> dict[str, Any]:
    cfg = kb_dir / ".engram" / "sources.json"
    if not cfg.exists():
        return {}
    try:
        data = json.loads(cfg.read_text())
    except json.JSONDecodeError:
        return {}
    g = (data.get("granola") or {}) if isinstance(data, dict) else {}
    return g if isinstance(g, dict) else {}


def _load_cache_path(kb_dir: Path) -> Path:
    g = _read_granola_config(kb_dir)
    cp = g.get("cache_path")
    if isinstance(cp, str) and cp.strip():
        return Path(cp).expanduser()
    # Resolve fresh each call so a Granola version bump (cache-v6 → v7) is
    # picked up without the user editing config.
    return _default_cache_path()


def _load_content_mode(kb_dir: Path) -> str:
    """Return the configured content_mode, defaulting to `both`.

    Unknown values warn and fall back to the default rather than crashing
    the whole sync — Granola is the user's most-personal source and a
    typo shouldn't kill it.
    """
    g = _read_granola_config(kb_dir)
    raw = g.get("content_mode")
    if raw is None:
        return DEFAULT_CONTENT_MODE
    if isinstance(raw, str) and raw.strip().lower() in VALID_CONTENT_MODES:
        return raw.strip().lower()
    print(
        f"[granola] unknown content_mode {raw!r}; "
        f"valid: {', '.join(VALID_CONTENT_MODES)}. Defaulting to "
        f"{DEFAULT_CONTENT_MODE!r}.",
        file=sys.stderr,
    )
    return DEFAULT_CONTENT_MODE


def _load_use_api(kb_dir: Path) -> bool:
    """Return whether the API path is enabled (default True).

    Only an explicit JSON `false` (or a falsy string like "false"/"no"/"0"
    /"off") disables it; missing keys, nulls, and anything else default
    to True so users get the richer content out of the box.
    """
    g = _read_granola_config(kb_dir)
    if "use_api" not in g:
        return True
    raw = g.get("use_api")
    if raw is False:
        return False
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("false", "no", "n", "0", "off", "disable", "disabled"):
            return False
        if s in ("true", "yes", "y", "1", "on", "enable", "enabled"):
            return True
    # null / unknown / True → True
    return True


def _load_workos_token() -> str | None:
    """Auto-detect Granola's WorkOS access token from the desktop app's
    session store. Returns None on any error so callers can degrade
    gracefully — never crashes the sync.
    """
    sb = GRANOLA_DIR / "supabase.json"
    if not sb.exists():
        return None
    try:
        outer = json.loads(sb.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(outer, dict):
        return None
    wt = outer.get("workos_tokens")
    if not isinstance(wt, str) or not wt.strip():
        return None
    try:
        inner = json.loads(wt)
    except json.JSONDecodeError:
        return None
    if not isinstance(inner, dict):
        return None
    tok = inner.get("access_token")
    if isinstance(tok, str) and tok.strip():
        return tok.strip()
    return None


def _load_api_token(kb_dir: Path) -> str | None:
    """Resolve the API token. Explicit `granola.api_token` in
    sources.json overrides the auto-detected one (handy for using a
    different account); else fall back to supabase.json."""
    g = _read_granola_config(kb_dir)
    explicit = g.get("api_token")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return _load_workos_token()


def _meta_path(kb_dir: Path) -> Path:
    return kb_dir / ".engram" / "granola-sync-meta.json"


def _load_meta(kb_dir: Path) -> dict[str, Any]:
    p = _meta_path(kb_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def _write_meta(kb_dir: Path, meta: dict[str, Any]) -> None:
    p = _meta_path(kb_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2) + "\n")


# ---- API client ------------------------------------------------------------

GRANOLA_API_BASE = "https://api.granola.ai"


class GranolaAPIError(RuntimeError):
    pass


class GranolaAPIAuthError(GranolaAPIError):
    pass


def _api_post(
    path: str,
    token: str,
    body: dict[str, Any],
    *,
    timeout: int = 20,
    max_retries: int = 4,
) -> Any:
    """POST to Granola's internal API with bearer auth and gzip handling.

    Returns the parsed JSON body. Raises:
      - GranolaAPIAuthError on 401/403
      - GranolaAPIError on other 4xx, exhausted 5xx retries, exhausted
        rate-limit retries, or persistent network errors

    Always sends `Accept-Encoding: gzip` and decompresses if the server
    actually applied gzip. Plain-text and JSON responses are also
    handled. Retries on 429 (Retry-After-aware) and 5xx with exp
    backoff.
    """
    url = GRANOLA_API_BASE + path
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": "engram-granola/0.1",
    }
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)

    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GranolaAPIError(
                        f"Granola POST {path} returned non-JSON body: {exc}"
                    ) from exc
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise GranolaAPIAuthError(
                    f"Granola rejected the access token ({exc.code}). "
                    f"Auth may have expired or the endpoint is paid-tier only."
                ) from exc
            if exc.code == 429:
                retry_after = float(exc.headers.get("Retry-After") or 0) or (2 ** attempt)
                attempt += 1
                if attempt > max_retries:
                    raise GranolaAPIError(
                        f"Granola rate-limit hit, retries exhausted on {path}"
                    ) from exc
                time.sleep(min(retry_after, 60))
                continue
            if 500 <= exc.code < 600 and attempt < max_retries:
                time.sleep(2 ** attempt)
                attempt += 1
                continue
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                err_body = ""
            raise GranolaAPIError(
                f"Granola POST {path} → {exc.code}: {err_body}"
            ) from exc
        except urllib.error.URLError as exc:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                attempt += 1
                continue
            raise GranolaAPIError(
                f"Granola POST {path} network error: {exc}"
            ) from exc


_AUTH_FAILURE_MESSAGE_PATTERNS = (
    "unsupported client",
    "invalid token",
    "unauthorized",
    "forbidden",
)
_unknown_panel_shape_logged = False


def _fetch_panels(doc_id: str, token: str) -> list[dict[str, Any]]:
    """Fetch the panel list for one meeting. Returns the raw list (may
    be empty for meetings without an enhanced summary).

    Granola occasionally signals auth failures with HTTP 200 + a body
    like ``{"message": "Unsupported client"}`` instead of a 401/403.
    Detect that shape and raise GranolaAPIAuthError so the caller's
    once-per-run latch fires and we don't poison the empty-panels cache.
    """
    global _unknown_panel_shape_logged
    resp = _api_post("/v1/get-document-panels", token, {"document_id": doc_id})
    if isinstance(resp, list):
        return resp
    # Defensive: some envelope shapes return {"panels": [...]}.
    if isinstance(resp, dict) and isinstance(resp.get("panels"), list):
        return resp["panels"]
    if isinstance(resp, dict):
        msg = resp.get("message")
        if isinstance(msg, str):
            low = msg.lower()
            if any(p in low for p in _AUTH_FAILURE_MESSAGE_PATTERNS):
                raise GranolaAPIAuthError(
                    f"Granola panels endpoint returned auth-failure body: {msg!r}"
                )
        if not _unknown_panel_shape_logged:
            print(
                f"[granola] panels endpoint returned unexpected dict shape "
                f"(keys={sorted(resp.keys())!r}); treating as empty.",
                file=sys.stderr,
            )
            _unknown_panel_shape_logged = True
    return []


def _pick_best_panel(panels: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the most-recently-updated non-deleted panel with non-empty
    HTML content. Returns None if nothing qualifies."""
    candidates = [
        p for p in panels
        if isinstance(p, dict)
        and not p.get("deleted_at")
        and isinstance(p.get("original_content"), str)
        and p["original_content"].strip()
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda p: str(
            p.get("content_updated_at")
            or p.get("updated_at")
            or p.get("created_at")
            or ""
        ),
    )


def _strip_panel_boilerplate(html: str) -> str:
    """Granola appends a `<hr>` followed by a 'Chat with meeting transcript'
    paragraph to its panel HTML. Cleave it off — it's chrome, not content.
    Tolerant to whitespace and minor markup variation."""
    if not html:
        return html
    # Cut at the first `<hr>` whose tail mentions "Chat with meeting"
    # (case-insensitive). If we don't see that exact tail, leave the
    # full HTML alone — better to keep too much than to drop content.
    m = re.search(r"<hr\s*/?>\s*<p[^>]*>\s*Chat with meeting transcript",
                  html, flags=re.IGNORECASE)
    if m:
        return html[:m.start()].rstrip()
    return html


# ---- cache parsing ---------------------------------------------------------

class GranolaCacheError(RuntimeError):
    pass


def load_cache(cache_path: Path) -> dict[str, Any]:
    """Parse Granola's cache-v3.json, handling both v5 and v6 wrappers.

    Raises GranolaCacheError with a clear message on any format surprise.
    """
    if not cache_path.exists():
        raise GranolaCacheError(
            f"Granola cache not found at {cache_path}. Is Granola installed? "
            f"If the cache lives elsewhere, set granola.cache_path in "
            f".engram/sources.json."
        )
    try:
        outer = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GranolaCacheError(f"cache-v3.json is not valid JSON: {exc}") from exc
    if not isinstance(outer, dict) or "cache" not in outer:
        raise GranolaCacheError("cache-v3.json missing top-level 'cache' key")

    cache_raw = outer["cache"]
    if isinstance(cache_raw, str):
        try:
            inner = json.loads(cache_raw)
        except json.JSONDecodeError as exc:
            raise GranolaCacheError(f"inner 'cache' string is not valid JSON: {exc}") from exc
    elif isinstance(cache_raw, dict):
        inner = cache_raw
    else:
        raise GranolaCacheError(
            f"'cache' key has unexpected type {type(cache_raw).__name__}; "
            f"expected string (pre-v6) or object (v6+)."
        )
    if "state" not in inner:
        raise GranolaCacheError("cache content missing 'state' key")
    return inner["state"]


def _iter_meetings(state: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield one merged meeting record per document, combining documents
    with metadata, transcripts, panels, and folder info.

    Mirrors GranolaMCP's merge logic so we match what community tools
    expect (docs override metadata on conflict; transcript_data and
    ai_summary_html added)."""
    documents = state.get("documents") or {}
    metadata = state.get("meetingsMetadata") or {}
    transcripts = state.get("transcripts") or {}
    panels = state.get("documentPanels") or {}
    lists = state.get("documentLists") or {}
    lists_meta = state.get("documentListsMetadata") or {}

    # Reverse folder index.
    doc_to_folder: dict[str, dict[str, str]] = {}
    for list_id, doc_ids in lists.items():
        folder_name = (lists_meta.get(list_id) or {}).get("title") or "Unknown"
        for did in doc_ids or []:
            doc_to_folder[did] = {"folder_id": list_id, "folder_name": folder_name}

    for doc_id, doc in documents.items():
        if not isinstance(doc, dict):
            continue
        meeting = dict(doc)

        for key, value in (metadata.get(doc_id) or {}).items():
            if key not in meeting or not meeting[key]:
                meeting[key] = value

        if doc_id in transcripts:
            meeting["transcript_data"] = transcripts[doc_id]

        doc_panels = panels.get(doc_id) or {}
        ai_summaries: list[str] = []
        panel_content: dict[str, Any] | None = None
        for pdata in doc_panels.values():
            oc = pdata.get("original_content") if isinstance(pdata, dict) else None
            if isinstance(oc, str) and oc.strip() and not oc.strip().startswith("<hr>"):
                ai_summaries.append(oc)
            if panel_content is None:
                pc = pdata.get("content") if isinstance(pdata, dict) else None
                if isinstance(pc, dict):
                    panel_content = pc
        if ai_summaries:
            meeting["ai_summary_html"] = "\n\n".join(ai_summaries)
        if panel_content is not None:
            meeting["panel_content"] = panel_content

        folder = doc_to_folder.get(doc_id) or {}
        if folder:
            meeting["folder_id"] = folder.get("folder_id")
            meeting["folder_name"] = folder.get("folder_name")

        # Canonical id for downstream code.
        meeting["_resolved_id"] = _pick(meeting, ["id", "meeting_id", "session_id", "uuid"], doc_id)
        yield meeting


def _pick(obj: dict[str, Any], keys: list[str], default: Any = "") -> Any:
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


# ---- field resolution ------------------------------------------------------

def _meeting_title(m: dict[str, Any]) -> str:
    t = _pick(m, ["title", "name", "subject", "meeting_name"], "")
    return str(t).strip() or "(untitled meeting)"


def _meeting_timestamp(m: dict[str, Any]) -> str:
    # Prefer finish/end time for chronology; fall back to start/created.
    ts = _pick(m, [
        "end_time", "endTime", "finished_at",
        "start_time", "startTime",
        "created_at", "timestamp", "date", "updated_at",
    ], "")
    if isinstance(ts, dict):
        # Google-calendar style {"dateTime": "..."} — rare but seen.
        ts = ts.get("dateTime") or ""
    return str(ts) if ts else ""


def _meeting_participants(m: dict[str, Any]) -> list[str]:
    for key in ("participants", "attendees", "users", "members"):
        val = m.get(key)
        if not val:
            continue
        out: list[str] = []
        if isinstance(val, list):
            for entry in val:
                if isinstance(entry, str):
                    out.append(entry)
                elif isinstance(entry, dict):
                    name = _pick(entry, ["name", "display_name", "email", "username"], "")
                    if name:
                        out.append(str(name))
        elif isinstance(val, dict):
            for entry in val.values():
                if isinstance(entry, dict):
                    name = _pick(entry, ["name", "display_name", "email", "username"], "")
                    if name:
                        out.append(str(name))
        if out:
            return out
    return []


def _meeting_duration_minutes(m: dict[str, Any]) -> int | None:
    d = _pick(m, ["duration_seconds", "duration", "length", "meeting_duration"], None)
    if d is None:
        return None
    try:
        secs = float(d)
    except (TypeError, ValueError):
        return None
    # Heuristic: if the number looks like milliseconds, convert down.
    if secs > 60 * 60 * 24:  # > 1 day is almost certainly ms
        secs = secs / 1000
    return max(1, int(round(secs / 60)))


# ---- HTML → markdown-with-headings ----------------------------------------

class _AISummaryParser(HTMLParser):
    """Flatten AI-summary HTML into block dicts compatible with
    chunk_by_headings. H1/H2 become heading blocks; everything else becomes
    paragraph blocks with prefixes for list items."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, str]] = []
        self._buf: list[str] = []
        self._current_block_type: str = "paragraph"
        self._list_stack: list[str] = []  # "ul" or "ol"
        self._ol_counters: list[int] = []
        self._in_li: bool = False

    def _flush(self) -> None:
        text = "".join(self._buf).strip()
        self._buf = []
        if not text:
            self._current_block_type = "paragraph"
            return
        self.blocks.append({"type": self._current_block_type, "plain_text": text})
        self._current_block_type = "paragraph"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in ("h1", "h2", "h3"):
            self._flush()
            self._current_block_type = {
                "h1": "heading_1",
                "h2": "heading_2",
                "h3": "heading_3",
            }[tag]
        elif tag in ("p", "div"):
            self._flush()
            self._current_block_type = "paragraph"
        elif tag == "br":
            self._buf.append("\n")
        elif tag in ("ul", "ol"):
            self._flush()
            self._list_stack.append(tag)
            if tag == "ol":
                self._ol_counters.append(1)
        elif tag == "li":
            self._flush()
            self._in_li = True
            indent = "  " * (len(self._list_stack) - 1) if self._list_stack else ""
            if self._list_stack and self._list_stack[-1] == "ol":
                n = self._ol_counters[-1] if self._ol_counters else 1
                self._buf.append(f"{indent}{n}. ")
                if self._ol_counters:
                    self._ol_counters[-1] += 1
            else:
                self._buf.append(f"{indent}- ")
        elif tag in ("strong", "b"):
            self._buf.append("**")
        elif tag in ("em", "i"):
            self._buf.append("*")
        elif tag == "code":
            self._buf.append("`")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("h1", "h2", "h3"):
            self._flush()
        elif tag in ("p", "div"):
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush()
            if self._list_stack:
                self._list_stack.pop()
            if tag == "ol" and self._ol_counters:
                self._ol_counters.pop()
        elif tag == "li":
            self._flush()
            self._in_li = False
        elif tag in ("strong", "b"):
            self._buf.append("**")
        elif tag in ("em", "i"):
            self._buf.append("*")
        elif tag == "code":
            self._buf.append("`")

    def handle_data(self, data: str) -> None:
        if data:
            self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def _summary_html_to_blocks(html: str) -> list[dict[str, str]]:
    if not html or not html.strip():
        return []
    p = _AISummaryParser()
    try:
        p.feed(html)
    except Exception:
        # If HTML is malformed, fall back to a plain-text single block.
        return [{"type": "paragraph", "plain_text": re.sub(r"<[^>]+>", "", html)}]
    p.close()
    return p.blocks


# ---- ProseMirror → blocks --------------------------------------------------

def _prosemirror_to_blocks(node: Any, depth: int = 0) -> list[dict[str, str]]:
    """Flatten a ProseMirror tree into heading/paragraph blocks."""
    out: list[dict[str, str]] = []
    if not isinstance(node, dict):
        return out
    ntype = node.get("type")
    content = node.get("content") or []

    if ntype == "doc":
        for c in content:
            out.extend(_prosemirror_to_blocks(c, depth))
        return out

    if ntype == "heading":
        level = ((node.get("attrs") or {}).get("level")) or 2
        text = _prosemirror_text(content).strip()
        if level == 1:
            out.append({"type": "heading_1", "plain_text": text})
        elif level == 2:
            out.append({"type": "heading_2", "plain_text": text})
        else:
            out.append({"type": "paragraph", "plain_text": f"**{text}**"})
        return out

    if ntype == "paragraph":
        text = _prosemirror_text(content).strip()
        if text:
            out.append({"type": "paragraph", "plain_text": text})
        return out

    if ntype in ("bullet_list", "bulletList"):
        for li in content:
            li_text = _prosemirror_text(li.get("content") or []).strip()
            if li_text:
                out.append({"type": "paragraph", "plain_text": f"{'  '*depth}- {li_text}"})
        return out

    if ntype in ("ordered_list", "orderedList"):
        for i, li in enumerate(content, 1):
            li_text = _prosemirror_text(li.get("content") or []).strip()
            if li_text:
                out.append({"type": "paragraph", "plain_text": f"{'  '*depth}{i}. {li_text}"})
        return out

    # Unknown container — recurse into children.
    for c in content:
        out.extend(_prosemirror_to_blocks(c, depth))
    return out


def _prosemirror_text(nodes: Any) -> str:
    if not isinstance(nodes, list):
        return ""
    parts: list[str] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        if n.get("type") == "text":
            t = n.get("text") or ""
            marks = {m.get("type") for m in (n.get("marks") or []) if isinstance(m, dict)}
            if "strong" in marks or "bold" in marks:
                t = f"**{t}**"
            if "em" in marks or "italic" in marks:
                t = f"*{t}*"
            if "code" in marks:
                t = f"`{t}`"
            parts.append(t)
        elif n.get("type") == "hard_break":
            parts.append("\n")
        else:
            # Recurse into inline containers (e.g. link wrappers).
            parts.append(_prosemirror_text(n.get("content") or []))
    return "".join(parts)


# ---- transcript helpers ----------------------------------------------------

def _transcript_text(segments: Any) -> str:
    """Concat transcript segments, one line per segment with speaker tag."""
    if not isinstance(segments, list):
        return ""
    lines: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = _pick(seg, ["text", "content"], "").strip()
        if not text:
            continue
        source = seg.get("source")  # "mic" | "system" | None
        speaker = "You" if source == "mic" else ("Other" if source == "system" else None)
        lines.append(f"{speaker}: {text}" if speaker else text)
    return "\n".join(lines)


# ---- main sync -------------------------------------------------------------

def sync(
    kb_dir: Path,
    *,
    full: bool = False,
    cache_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Parse Granola's local cache and return a list of Item JSONs."""
    if sys.platform != "darwin":
        print(
            "[granola] skipped: Granola is macOS-only (platform is "
            f"{sys.platform!r}).",
            file=sys.stderr,
        )
        return []

    cache_path = cache_path or _load_cache_path(kb_dir)

    content_mode = _load_content_mode(kb_dir)
    use_api = _load_use_api(kb_dir)
    token = _load_api_token(kb_dir) if use_api else None
    print(
        f"[granola] content_mode={content_mode!r} use_api={use_api} "
        f"token={'present' if token else 'missing'}",
        file=sys.stderr,
    )

    meta = _load_meta(kb_dir)
    prior_mode = meta.get("content_mode")
    prior_use_api = meta.get("use_api")
    # If the configured mode OR use_api changed since the last successful
    # sync, the incremental cursor is no longer trustworthy: items.jsonl
    # may hold chunks from streams or sources the user just disabled.
    # Force a full re-sync and purge ALL granola items so meetings now
    # skipped under the new config don't leave stale chunks behind.
    # A missing prior value means this is the first sync since that
    # field was introduced — treat as a match. Done before cache load
    # so a tightening config change still purges stale items even if
    # the cache is temporarily unavailable.
    mode_changed = prior_mode is not None and prior_mode != content_mode
    use_api_changed = prior_use_api is not None and bool(prior_use_api) != use_api
    config_changed = mode_changed or use_api_changed
    if config_changed:
        reasons: list[str] = []
        if mode_changed:
            reasons.append(f"content_mode {prior_mode!r}→{content_mode!r}")
        if use_api_changed:
            reasons.append(f"use_api {prior_use_api}→{use_api}")
        print(
            f"[granola] config changed ({'; '.join(reasons)}); forcing "
            f"full re-sync and clearing stale granola items.",
            file=sys.stderr,
        )
        drop_items_by_id_prefix(kb_dir, f"{SOURCE_ID}:")
        full = True

    try:
        state = load_cache(cache_path)
    except GranolaCacheError as exc:
        print(f"[granola] {exc}", file=sys.stderr)
        # Even on cache miss, persist the active config so the next
        # sync doesn't see a stale prior config and re-trigger the purge.
        if config_changed:
            existing = dict(meta)
            existing["content_mode"] = content_mode
            existing["use_api"] = use_api
            _write_meta(kb_dir, existing)
        return []

    last_synced_at = None if full else meta.get("last_synced_at")

    known_ids: set[str] = set()
    if last_synced_at:
        for it in load_items(kb_dir / "raw" / "items.jsonl"):
            if it.get("source") == SOURCE_ID:
                mid = (it.get("metadata") or {}).get("meeting_id")
                if mid:
                    known_ids.add(mid)
        print(
            f"[granola] incremental: {len(known_ids)} known meeting(s); "
            f"re-syncing any updated since {last_synced_at} or not yet indexed",
            file=sys.stderr,
        )
    else:
        print(f"[granola] full sync from {cache_path}", file=sys.stderr)

    # Per-run API state. `available` flips False after the first 401/403
    # so we don't keep hammering the panels endpoint for every meeting.
    api_state: dict[str, Any] = {
        "available": bool(use_api and token),
        "auth_error_logged": False,
        "panels_fetched": 0,
        "meetings_skipped": 0,
    }
    if use_api and not token:
        print(
            "[granola] use_api=true but no token found in supabase.json or "
            "granola.api_token; falling back to local-only.",
            file=sys.stderr,
        )

    # Persisted cache: meetings we previously fetched and saw [] for.
    # Keyed by meeting_id → doc.updated_at at the time of that empty
    # result. Lets us skip the network on subsequent runs unless the
    # doc has been edited since.
    panel_cache: dict[str, str] = dict(meta.get("meetings_without_panels") or {})

    all_items: list[Item] = []
    seen = 0
    changed = 0
    for meeting in _iter_meetings(state):
        seen += 1
        meeting_id = str(meeting["_resolved_id"])
        # Use the same timestamp ladder as _meeting_timestamp so the
        # incremental filter agrees with what we emit. If a known meeting
        # has no timestamp, assume unchanged and skip.
        updated = _meeting_timestamp(meeting)
        if last_synced_at and meeting_id in known_ids:
            if not updated or str(updated) <= last_synced_at:
                continue

        items = _build_items_for_meeting(
            meeting,
            content_mode=content_mode,
            token=token,
            use_api=use_api,
            api_state=api_state,
            panel_cache=panel_cache,
        )
        if not items:
            continue

        drop_items_by_id_prefix(kb_dir, f"{SOURCE_ID}:{meeting_id}:")
        all_items.extend(items)
        changed += 1

    # Garbage-collect panel_cache entries for meetings that no longer
    # exist in the local cache (e.g. deleted upstream).
    live_ids = {str(m["_resolved_id"]) for m in _iter_meetings(state)}
    panel_cache = {k: v for k, v in panel_cache.items() if k in live_ids}

    now = datetime.now(timezone.utc).isoformat()
    _write_meta(kb_dir, {
        "last_synced_at": now,
        "content_mode": content_mode,
        "use_api": use_api,
        "last_run_meetings_seen": seen,
        "last_run_meetings_changed": changed,
        "last_run_api_panels_fetched": api_state.get("panels_fetched", 0),
        "last_run_api_meetings_skipped": api_state.get("meetings_skipped", 0),
        "meetings_without_panels": panel_cache,
        "cache_path": str(cache_path),
    })
    print(
        f"[granola] {seen} meeting(s) seen · {changed} changed · "
        f"{len(all_items)} chunk(s) emitted "
        f"(api: {api_state.get('panels_fetched', 0)} fetched, "
        f"{api_state.get('meetings_skipped', 0)} empty)",
        file=sys.stderr,
    )
    return [it.to_json() for it in all_items]


def _build_notes_chunks(meeting: dict[str, Any]) -> tuple[list, str]:
    """Return (chunks, content_part_label) for the local-cache notes
    stream. Labels: "ai_summary" if a (rare) cached panel HTML survives,
    else "notes". Chunks may be empty if no content is parseable.

    The API-driven path is handled separately by
    `_build_api_summary_chunks` and supersedes this when available.
    """
    summary_html = meeting.get("ai_summary_html") or ""
    notes = meeting.get("notes") or meeting.get("panel_content")

    blocks: list[dict[str, str]] = []
    label = "notes"
    if summary_html.strip():
        blocks = _summary_html_to_blocks(summary_html)
        label = "ai_summary"
    elif isinstance(notes, dict) and notes:
        blocks = _prosemirror_to_blocks(notes)
        label = "notes"

    if not blocks:
        return [], label

    chunks = chunk_by_headings(blocks, heading_levels=("heading_1", "heading_2"))
    if len(chunks) == 1 and chunks[0].title is None and len(chunks[0].body) > 6000:
        chunks = chunk_by_size(chunks[0].body, max_chars=4000)
    if not any(c.body.strip() for c in chunks):
        return [], label
    return chunks, label


def _build_api_summary_chunks(html: str) -> list:
    """Convert API-fetched panel HTML into chunks. Granola's panels use
    `<h3>` as their top-level section header (not h1/h2), so we split
    on h1/h2/h3 here. Trailing 'Chat with meeting transcript' boilerplate
    is stripped first.
    """
    if not html or not html.strip():
        return []
    cleaned = _strip_panel_boilerplate(html)
    blocks = _summary_html_to_blocks(cleaned)
    if not blocks:
        return []
    chunks = chunk_by_headings(
        blocks,
        heading_levels=("heading_1", "heading_2", "heading_3"),
    )
    if len(chunks) == 1 and chunks[0].title is None and len(chunks[0].body) > 6000:
        chunks = chunk_by_size(chunks[0].body, max_chars=4000)
    if not any(c.body.strip() or c.title for c in chunks):
        return []
    return chunks


def _notes_body_chars(chunks: list) -> int:
    """Count rendered body chars across notes chunks, excluding chunks
    that are pure heading (title-only, empty body). Used by `auto`."""
    total = 0
    for c in chunks:
        body = (c.body or "").strip()
        if body:
            total += len(body)
    return total


def _build_transcript_chunks(meeting: dict[str, Any]) -> list:
    transcript_text = _transcript_text(meeting.get("transcript_data"))
    if not transcript_text.strip():
        return []
    return chunk_by_size(transcript_text, max_chars=4000)


def _try_fetch_api_summary(
    meeting: dict[str, Any],
    *,
    token: str | None,
    use_api: bool,
    api_state: dict[str, Any],
    panel_cache: dict[str, str],
) -> tuple[list, str | None]:
    """If the API path is enabled and viable for this meeting, fetch
    panels and return (chunks, panel_updated_at). Returns ([], None) when
    we should fall back to local notes.

    `api_state` is a per-run mutable dict tracking auth-failed state so
    we don't keep retrying once a 401/403 fires:
        {"available": bool, "auth_error_logged": bool}

    `panel_cache` is the persisted `meetings_without_panels` map: doc_id
    → doc.updated_at when we last saw [] from the API. If the meeting
    hasn't been updated since then, we skip the network call entirely.
    """
    if not use_api or not token:
        return [], None
    if not api_state.get("available", True):
        return [], None
    meeting_id = str(meeting["_resolved_id"])
    doc_updated_at = str(meeting.get("updated_at") or meeting.get("content_updated_at") or "")
    cached_at = panel_cache.get(meeting_id)
    if cached_at and doc_updated_at and doc_updated_at <= cached_at:
        # We previously fetched and got [] for this meeting; doc hasn't
        # changed → skip the API call.
        return [], None

    try:
        panels = _fetch_panels(meeting_id, token)
    except GranolaAPIAuthError as exc:
        if not api_state.get("auth_error_logged"):
            print(
                f"[granola] API auth failed ({exc}); falling back to "
                f"local-only for the rest of this run.",
                file=sys.stderr,
            )
            api_state["auth_error_logged"] = True
        api_state["available"] = False
        return [], None
    except GranolaAPIError as exc:
        # Per-meeting API error — don't blow up the whole run, just log
        # and let this meeting fall back to local notes. Latch the log
        # so a network outage with N meetings doesn't spam N near-identical
        # lines; non-auth errors don't disable the API for the rest of
        # the run though (a transient timeout shouldn't punish later
        # meetings — just stop being noisy about it).
        if not api_state.get("panel_error_logged"):
            print(
                f"[granola] panel fetch failed for {meeting_id}: {exc} "
                f"(suppressing further panel-fetch errors this run)",
                file=sys.stderr,
            )
            api_state["panel_error_logged"] = True
        return [], None

    best = _pick_best_panel(panels)
    if best is None:
        # Empty / all-deleted → mark in cache so next run skips the network.
        if doc_updated_at:
            panel_cache[meeting_id] = doc_updated_at
        api_state["meetings_skipped"] = api_state.get("meetings_skipped", 0) + 1
        return [], None

    chunks = _build_api_summary_chunks(best.get("original_content") or "")
    if not chunks:
        if doc_updated_at:
            panel_cache[meeting_id] = doc_updated_at
        return [], None

    api_state["panels_fetched"] = api_state.get("panels_fetched", 0) + 1
    panel_updated_at = (
        best.get("content_updated_at")
        or best.get("updated_at")
        or best.get("created_at")
    )
    # Drop any prior empty-cache entry — we now have content.
    panel_cache.pop(meeting_id, None)
    return chunks, panel_updated_at


def _build_items_for_meeting(
    meeting: dict[str, Any],
    *,
    content_mode: str = DEFAULT_CONTENT_MODE,
    token: str | None = None,
    use_api: bool = False,
    api_state: dict[str, Any] | None = None,
    panel_cache: dict[str, str] | None = None,
) -> list[Item]:
    """Build chunked Items for a meeting per the given content_mode.

    Modes (see module docstring): notes | transcript | both | auto.
    Falls back gracefully when the chosen stream is empty:
      - notes mode with no notes → empty (meeting skipped).
      - transcript mode with no transcript → empty.
      - both with no notes → emit transcript-only (and vice versa).
      - auto picks notes if substantive, else transcript, else
        whatever single stream exists.

    When `use_api` is True and a `token` is available, the AI-summary
    path (Granola's internal API) is tried first for non-transcript
    modes; if it returns content, it supersedes the local-cache notes.
    """
    if api_state is None:
        api_state = {"available": True, "auth_error_logged": False}
    if panel_cache is None:
        panel_cache = {}

    meeting_id = str(meeting["_resolved_id"])
    title = _meeting_title(meeting)
    ts = _meeting_timestamp(meeting)
    participants = _meeting_participants(meeting)
    duration = _meeting_duration_minutes(meeting)
    folder = meeting.get("folder_name")

    preamble_parts: list[str] = []
    if participants:
        who = ", ".join(participants[:6])
        if len(participants) > 6:
            who += f" (+{len(participants) - 6} more)"
        preamble_parts.append(f"Participants: {who}")
    if duration is not None:
        preamble_parts.append(f"Duration: {duration} min")
    if folder:
        preamble_parts.append(f"Folder: {folder}")
    preamble = " · ".join(preamble_parts) or None

    base_meta_common: dict[str, Any] = {
        "meeting_id": meeting_id,
        "meeting_title": title,
        "meeting_date": ts,
        "participants": participants,
        "duration_minutes": duration,
        "folder_name": folder,
    }
    timestamp = str(ts) if ts else ""

    # Resolve which streams to emit based on mode.
    notes_chunks: list = []
    notes_label = "notes"
    transcript_chunks: list = []

    # API summary takes priority over local notes for non-transcript modes.
    api_chunks: list = []
    if content_mode != "transcript":
        api_chunks, _panel_updated_at = _try_fetch_api_summary(
            meeting,
            token=token,
            use_api=use_api,
            api_state=api_state,
            panel_cache=panel_cache,
        )

    if content_mode == "transcript":
        transcript_chunks = _build_transcript_chunks(meeting)
    elif content_mode == "notes":
        if api_chunks:
            notes_chunks, notes_label = api_chunks, "ai_summary"
        else:
            notes_chunks, notes_label = _build_notes_chunks(meeting)
    elif content_mode == "auto":
        if api_chunks:
            notes_chunks, notes_label = api_chunks, "ai_summary"
        else:
            notes_chunks, notes_label = _build_notes_chunks(meeting)
        body_chars = _notes_body_chars(notes_chunks)
        if body_chars >= AUTO_NOTES_BODY_THRESHOLD:
            # Notes is substantive — emit notes only, no transcript.
            transcript_chunks = []
        else:
            # Sub-threshold notes (including the empty-H1-only case where
            # notes_chunks is truthy but body_chars == 0): prefer transcript
            # if present, otherwise fall back to whatever notes we have as
            # a last resort rather than skipping the meeting entirely.
            transcript_chunks = _build_transcript_chunks(meeting)
            if transcript_chunks:
                notes_chunks = []
            # else: only thin notes survives — emit it as last resort.
    else:  # "both" (and any unknown defensively defaulted upstream)
        if api_chunks:
            notes_chunks, notes_label = api_chunks, "ai_summary"
        else:
            notes_chunks, notes_label = _build_notes_chunks(meeting)
        transcript_chunks = _build_transcript_chunks(meeting)

    if not notes_chunks and not transcript_chunks:
        return []

    # Stitch notes chunks then transcript chunks into a single Item-id
    # family. We can't reuse make_chunk_items twice (each call resets
    # chunk_index from 0), so build Items directly.
    notes_total = len(notes_chunks)
    transcript_total = len(transcript_chunks)
    total = notes_total + transcript_total

    out: list[Item] = []
    next_index = 0

    def _emit(chunk, *, content_part: str) -> None:
        nonlocal next_index
        idx = next_index
        next_index += 1

        lines: list[str] = []
        if title:
            lines.append(f"# {title}")
        if chunk.title:
            lines.append(f"## {chunk.title}")
        if lines:
            lines.append("")
        if idx == 0 and preamble:
            lines.append(preamble.strip())
            lines.append("")
        if chunk.body:
            lines.append(chunk.body)
        text = "\n".join(lines).strip()

        meta = dict(base_meta_common)
        meta.update({
            "parent_id": meeting_id,
            "parent_title": title,
            "chunk_index": idx,
            "chunk_count": total,
            "chunk_title": chunk.title,
            "content_part": content_part,
            # Back-compat: content_source used to identify which stream
            # the meeting was rendered from. Keep it set to the same
            # label for downstream code that still reads it.
            "content_source": content_part,
        })

        out.append(
            Item(
                id=f"{SOURCE_ID}:{meeting_id}:{idx}",
                source=SOURCE_ID,
                text=text,
                timestamp=timestamp,
                author=None,
                url=None,
                metadata=meta,
            )
        )

    for c in notes_chunks:
        _emit(c, content_part=notes_label)
    for c in transcript_chunks:
        _emit(c, content_part="transcript")

    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Dump Granola meetings as chunked Items.")
    ap.add_argument("--kb", type=Path, default=Path.cwd())
    ap.add_argument("--cache", type=Path, default=None, help="Override cache-v3.json path")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    items = sync(args.kb, full=args.full, cache_path=args.cache)
    print(f"[granola] {len(items)} item(s) total")
    for it in items[: args.limit]:
        m = it.get("metadata") or {}
        print(
            f"  {it['id']} · {m.get('meeting_title')!r} "
            f"chunk {m.get('chunk_index')}/{m.get('chunk_count')} "
            f"({m.get('content_source')}) · {len(it['text'])} chars"
        )
