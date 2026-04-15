# End-to-end test report — personal-wiki branch

Test KB: `/private/tmp/twkb-e2e/` (fresh scaffold). Tests run 2026-04-15.
All issues below are reproducible from a clean state.

---

## What works ✅

| Source | Items | Notes |
|---|---|---|
| `init.py` scaffold | — | Creates `CLAUDE.md`, `.twitter-wiki/`, `wiki/`, `notes/`, `raw/bookmarks/` cleanly. |
| `--source x` | 1063 bookmarks | Cookies extracted from Brave; full sync OK. |
| `--source claude-code` | 177 Q+A pairs | Walks `~/.claude/projects/`, dedupes by folder+session. |
| `--source browser-bookmarks` | 38 URLs | Reads Chrome/Brave/Edge JSON. |
| `--source github-stars` (with handle) | 1 repo | Public API works; graceful skip when handle missing. |
| `--source kindle --clippings` | 2 highlights | Synthetic `My Clippings.txt` parsed correctly. |
| `--source claude-ai` | 352 Q+A pairs from 63 convs | First live test passed end-to-end. |
| `--source chatgpt` (auth/list/fetch path) | 200/247 in run 1 | Auth + pagination + Q+A pairing all proven. |
| `/kb-import-chatgpt` (synthetic zip) | 1 pair | `_pair_turns` reused cleanly. |
| `/kb-import-claude` (synthetic zip) | 1 pair | Works on both flat and nested `conversations.json`. |
| `--source all` | All sources run | Per-source try/except isolates failures correctly. |
| `preprocess.py` on mixed corpus | 1634 items routed | Handles all source types; deterministic across runs. |
| `preprocess.py` per-source counts | shown in manifest | New `sources:` summary line works. |
| Item id stability | dedupe across re-syncs verified | `merge_items` keys on `id` correctly. |

## What is broken ❌

### Bug 1 — Cluster-map `sources`-only filter matches zero items
**Severity:** High. Documented behavior is broken.

`references/clustering-guide.md` says `sources` is an AND gate over
the OR of other rules, and "omit `sources` to match every source" —
implying that a topic with **only** `sources: ["claude-code"]` should
match all claude-code items.

In practice the topic matches nothing:

```json
{"name": "claude-code-sessions", "match": {"sources": ["claude-code"]}}
{"name": "ai-chats", "match": {"sources": ["chatgpt", "claude-ai"]}}
{"name": "kindle-highlights", "match": {"sources": ["kindle"]}}
```

→ `claude-code-sessions: 0`, `ai-chats: 0`, `kindle-highlights: 0`.

**Root cause:** `Topic.matches()` in `scripts/preprocess.py:70` returns
`False` unless at least one positive rule (authors / keywords /
hashtags / regexes) hits. The source filter is treated as a *gate
that can only reject*, never as a sufficient match condition on its
own. So source-only topics are unreachable.

**Fix direction:** when `self.sources` is set and no other positive
rules are configured, treat a source match as sufficient.

---

### Bug 2 — `import_export.py` uncaught `BadZipFile` traceback
**Severity:** Medium. Bad UX.

```
$ echo "test" > /tmp/notazip.zip
$ ./import_export.py --provider chatgpt --zip /tmp/notazip.zip --kb /tmp/twkb-e2e
Traceback (most recent call last):
  ...
zipfile.BadZipFile: File is not a zip file
```

A user who points at a `.zip.part`, a corrupt download, or any
non-zip file gets a Python traceback instead of a clean error.

**Fix direction:** catch `zipfile.BadZipFile` in
`sources/chatgpt.py:ingest_export` and `sources/claude_ai.py:ingest_export`
(or once in `import_export.py:main`) and re-raise as a `ValueError` with
a clear message.

---

### Bug 3 — ChatGPT 403 surfaced as "session cookie is stale"
**Severity:** High. Misleading error sends users down the wrong fix path.

Live test: an hour after a successful 200/247 fetch, ChatGPT's
`/api/auth/session` returned `403` with a Cloudflare HTML challenge
page in the body. The code blamed the cookie (which was still valid
when freshly extracted from the browser).

**Root cause:** `_get_access_token` and `_request` lump 401 and 403
together as `ChatGPTAuthError("session cookie is stale")`. 403 with
an HTML body is almost always Cloudflare bot mitigation, not auth
failure — but the user is told to log out and back in, which won't
help.

**Fix direction:** in `sources/chatgpt.py:_request`, sniff the body —
if the response body contains `<html` or known Cloudflare markers,
raise a distinct `ChatGPTBlockedError("blocked by Cloudflare; wait
30+ minutes or use /kb-import-chatgpt with an export zip")`. Same
treatment for claude_ai.

---

### Bug 4 — No graceful resumption after ChatGPT block
**Severity:** Medium. Partial-progress design only handles 429.

The 429 path is good (we sort oldest-first, persist `lastUpdateTime`
mid-run, so reruns pick up where we left off). The 403/CF path
raises *before* any conversations are fetched (the block hits
`/api/auth/session`), so no progress is recorded and there's no
fallback besides "wait it out or use the export zip."

**Fix direction:** Tied to Bug 3. If `_get_access_token` is blocked,
print a clearer "you're in a Cloudflare cooldown — wait or use
import-zip" message instead of a generic 403.

---

### Bug 5 — X sync stdout summary uses ugly relative path
**Severity:** Low. Cosmetic.

```
sync complete: 1063 new, 1063 total → ../../private/tmp/twkb-e2e/raw/bookmarks.jsonl
```

When the KB lives under a symlinked path (e.g. `/tmp` → `/private/tmp`
on macOS), `os.path.relpath` walks up out of `cwd`. The `os.path.relpath`
fallback in `scripts/sync.py:274` was meant to *avoid* a crash but
produces a nonsense path.

**Fix direction:** when relpath has a leading `..`, fall back to the
absolute path. Or just always print the absolute path.

---

### Bug 6 — Per-source failures don't print a `sync complete` line
**Severity:** Low. Inconsistent stdout contract.

Other sources print `sync complete: <source> → N items` to stdout on
success. ChatGPT/Claude.ai failure path returns rc=4/5 silently from
the dispatcher's perspective — only stderr has output. A `--source all`
run shows a mix of "complete" lines for some sources and silence for
the failed one. Hard to script around.

**Fix direction:** print a `sync failed: chatgpt → <reason>` line on
stdout in `_sync_chatgpt` / `_sync_claude_ai` failure branches.

---

### Bug 7 — `kb-add-source.md` doesn't mention chatgpt or claude-ai
**Severity:** Low. Discoverability gap.

The command is the documented entry point for configuring a new source.
ChatGPT and Claude.ai don't need any config (just cookies), but they
should at least be listed so users find them. Same for `--source` help
in `sync.py` — already updated, but the dedicated `/kb-add-source`
doc was missed.

**Fix direction:** add brief sections for `chatgpt` and `claude-ai` to
`commands/kb-add-source.md` — "no config needed; run `/kb-sync --source
chatgpt`" and a privacy note.

---

### Bug 8 — `SKILL.md` and `README.md` don't reference the new sources
**Severity:** Medium. New users won't discover the new capabilities.

`grep -i 'chatgpt\|claude\.ai\|claude-ai' SKILL.md README.md` → no hits.
Anyone reading the skill description still sees a Twitter-bookmarks-only
product.

**Fix direction:** update SKILL.md's source list and README's positioning
paragraph. Likely batched with the eventual rebrand to `personal-wiki`.

---

## Untestable in this pass ⚠️

These need real artifacts I don't have:

- **Real ChatGPT export zip** — only synthetic data tested. Format may
  differ in ways our `_pair_turns` doesn't anticipate (e.g. tool-use
  blocks, image refs, deleted messages).
- **Real Claude.ai export zip** — same caveat. The export's
  `conversations.json` location may be nested differently than my
  synthetic mock.
- **`/kb-enable-autosync` end-to-end** — depends on Claude Code's
  `CronCreate` tool firing the scheduled command. The slash-command
  doc is correct; the actual cron firing should be verified manually
  with `--every 5m` on a test KB.
- **ChatGPT full sync** — blocked by current Cloudflare cooldown. Need
  to wait several hours and retry to confirm the partial-progress
  cursor actually picks up from item 201.
- **Stale-cookie path on Claude.ai** — only tested the happy path.
- **`extract_cookies` for Edge / Chrome on a clean machine** — I only
  tested with Brave (where the user is logged in).

---

## Recommended fix order

1. **Bug 1** (cluster-map `sources` filter) — blocks Phase 2 source
   routing entirely; without it, the AI-chat/Kindle/etc. items just
   leak into `_unsorted`.
2. **Bug 3 + 4** (ChatGPT 403 misclassification) — biggest UX trap;
   users hitting Cloudflare get pointed at the wrong fix.
3. **Bug 2** (BadZipFile traceback) — quick polish, makes the
   fallback path trustworthy.
4. **Bug 6** (silent failure stdout) — makes `--source all` scriptable.
5. **Bug 5** (relpath cosmetic) — drive-by.
6. **Bug 7 + 8** (docs) — needed before public distribution.

After fixes: live-rerun `--source all`, then a real ChatGPT export
zip ingest, then `/kb-enable-autosync --every 5m` for one hour to
verify the cron path.
