---
description: Cluster bookmarks and synthesize wiki pages from the current KB
argument-hint: [topic-name]
---

The user wants to ingest bookmarks into the wiki. The current working directory should be a twitter-wiki KB (it should contain a `CLAUDE.md` and a `.twitter-wiki/` subdirectory). If it doesn't, tell the user to `cd` into their KB first or run `/kb-init` to scaffold one.

Also confirm `raw/bookmarks.jsonl` exists and is non-empty. If it's missing or empty, tell the user to run `/kb-sync` first and stop.

Then follow the **Ingest workflow** in SKILL.md end to end. In particular:

- If `.twitter-wiki/cluster-map.json` does not exist, do the bootstrap step (sample bookmarks, derive topics, write the map, confirm with the user) before running preprocess.
- Run `~/.claude/skills/twitter-wiki/.venv/bin/python ~/.claude/skills/twitter-wiki/scripts/preprocess.py --kb $(pwd)` once the map is in place.
- Synthesize or update wiki pages per the workflow, consulting `ingest-state.json` to skip batches that haven't grown.

If `$ARGUMENTS` names a single topic (kebab-case), scope the synthesis step to just that topic's batch — still run preprocess in full, but only (re)write `wiki/<topic>.md` and refresh `index.md` / `log.md` accordingly. Otherwise process all changed batches.

When done, report what was created vs updated vs skipped, and flag anything in `_unsorted.md` worth a new topic. Do NOT auto-run `/kb-lint` — suggest it if you noticed issues.
