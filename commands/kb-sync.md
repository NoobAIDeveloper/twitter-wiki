---
description: Sync new bookmarks from your logged-in X session into this KB
argument-hint: [--browser auto|chrome|brave|edge] [--full] [--max-pages N]
---

The user wants to sync their Twitter/X bookmarks into the current KB. The current working directory should be a twitter-wiki KB (it should contain a `CLAUDE.md` and a `.twitter-wiki/` subdirectory). If it doesn't, tell the user to `cd` into their KB first or run `/kb-init` to scaffold one.

Run the sync script:

```bash
~/.claude/skills/twitter-wiki/.venv/bin/python ~/.claude/skills/twitter-wiki/scripts/sync.py --kb $(pwd) $ARGUMENTS
```

The script handles browser cookie extraction, GraphQL pagination, dedupe, and writing to `raw/bookmarks.jsonl`. Its stdout is a one-line summary you can pass through. Its stderr is verbose progress — surface only the important parts to the user (added count, total, errors).

After it finishes:

- **Success with new bookmarks:** report the count and suggest `/kb-ingest` to add them to the wiki.
- **Success with no new bookmarks:** say so plainly and stop.
- **Auth error (cookies stale):** tell the user to log out and back into X in their browser, then retry.
- **No browser found:** tell the user which browsers we look for (Chrome, Brave, Edge on macOS/Linux) and that they need to be logged into X.
- **Rate limited:** tell the user to retry later; the script already retried with backoff.

Do NOT auto-run `/kb-ingest` after sync — let the user choose. But make the suggestion explicit when there's new content.
