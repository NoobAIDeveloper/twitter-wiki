---
description: Configure a new data source (GitHub, etc.) for this KB
argument-hint: <source-name> [source-specific args]
---

The user wants to enable a new data source for the current KB. Supported source names: `github-stars`, `claude-code`, `browser-bookmarks`, `kindle`, `chatgpt`, `claude-ai`.

The current working directory should be a KB (contain `CLAUDE.md`). If not, tell them to `cd` into their KB first or run `/kb-init`.

## How to configure each source

Read `$(pwd)/.twitter-wiki/sources.json` (create if missing, JSON object root). Add the relevant block, then commit with an atomic write.

### `github-stars`
Ask the user for their GitHub handle if `$ARGUMENTS` didn't include one. Merge into `sources.json`:

```json
{"github": {"handle": "their-handle"}}
```

Optionally mention: setting `GITHUB_TOKEN` env var raises the rate limit from 60/hr to 5000/hr. They can do this in their shell profile — don't ask them to paste a token here.

After updating `sources.json`, tell them to run `/kb-sync --source github-stars`.

### `claude-code`
No config required — the adapter discovers sessions automatically. Just tell them to run `/kb-sync --source claude-code`. Warn that by default sessions from this KB's own directory are skipped; they can override with `--include-self`.

### `browser-bookmarks`
No config required. Tell them to run `/kb-sync --source browser-bookmarks`.

### `kindle`
One-shot import, not an ongoing sync. Tell the user to:
1. Plug their Kindle in via USB.
2. Find the `My Clippings.txt` file on the Kindle drive.
3. Run `/kb-sync --source kindle --clippings /path/to/My\ Clippings.txt`.

### `chatgpt`
No config required — cookies are extracted from the user's browser. Tell them to:
1. Make sure they're logged in at https://chatgpt.com in their default browser.
2. Run `/kb-sync --source chatgpt`.

Privacy note: pulls Q+A pairs from the user's ChatGPT history into `raw/items.jsonl`. If the live sync ever breaks (Cloudflare, cookie expiry), the fallback is `/kb-request-chatgpt-export` → `/kb-import-chatgpt <zip>`.

### `claude-ai`
No config required — same cookie-based approach as ChatGPT. Tell them to:
1. Make sure they're logged in at https://claude.ai.
2. Run `/kb-sync --source claude-ai`.

Fallback if live sync breaks: export from Settings → Account → Export Account Data, then run `/kb-import-claude <zip>`.

## After configuring

Don't automatically run the sync — let the user trigger it. But say exactly which `/kb-sync` invocation they should use next.
