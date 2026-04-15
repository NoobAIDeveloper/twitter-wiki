---
description: Configure a new data source (GitHub, etc.) for this KB
argument-hint: <source-name> [source-specific args]
---

The user wants to enable a new data source for the current KB. Supported source names: `github-stars`, `claude-code`, `browser-bookmarks`, `kindle`.

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

## After configuring

Don't automatically run the sync — let the user trigger it. But say exactly which `/kb-sync` invocation they should use next.
