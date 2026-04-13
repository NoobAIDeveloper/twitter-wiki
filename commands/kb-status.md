---
description: Show sync and ingest stats for the current KB
---

The user wants a quick status readout. The current working directory should be a twitter-wiki KB (it should contain a `CLAUDE.md` and a `.twitter-wiki/` subdirectory). If it doesn't, tell the user to `cd` into their KB first or run `/kb-init` to scaffold one.

There's no dedicated stats script — compute it yourself from files on disk:

- **Last sync**: `lastSyncAt` and `totalBookmarks` from `.twitter-wiki/sync-meta.json` (or "never synced" if missing).
- **Bookmark count**: line count of `raw/bookmarks.jsonl` (treat missing / empty as 0).
- **Topics**: count and names from `.twitter-wiki/cluster-map.json`, or "not clustered yet" if the file is missing.
- **Wiki pages**: count of `wiki/*.md` (exclude `index.md`, `log.md`, and anything under `wiki/queries/`, which you can report separately).
- **Last ingest**: newest timestamp from `.twitter-wiki/ingest-state.json`, or "never ingested" if missing.

Present it as a short bulleted summary. Then surface the obvious next action:

- If `sync-meta.json` is older than a few days, suggest `/kb-sync`.
- If `totalBookmarks` in `sync-meta.json` exceeds what `ingest-state.json` reflects (or if ingest-state is missing), suggest `/kb-ingest`.
- If `_unsorted.md` exists and has a non-trivial count, mention it and suggest `/kb-recluster`.

The `.twitter-wiki/cluster-map.json` can be parsed with Python's stdlib `json` module. For the other JSON files, just read them directly.

Read-only command — don't write anything.
