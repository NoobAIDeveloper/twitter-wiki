---
description: Scaffold a new twitter-wiki knowledge base at the given path
argument-hint: <path> [--no-obsidian] [--no-git] [--force]
---

The user wants to scaffold a new twitter-wiki KB.

Run the init script:

```bash
~/.claude/skills/twitter-wiki/.venv/bin/python ~/.claude/skills/twitter-wiki/scripts/init.py $ARGUMENTS
```

If `$ARGUMENTS` is empty, ask the user where they want the KB to live (suggest `~/twitter-wiki` as a default). Don't proceed without an explicit path.

After the script succeeds, briefly tell the user what was created and the next steps (`/kb-sync` then `/kb-ingest`). Do NOT auto-run the next steps — the user should `cd` into the new KB and start a fresh Claude session in there first.

If the script fails because `CLAUDE.md` already exists, tell the user and ask whether they want to use `--force` (warn that this overwrites their CLAUDE.md but leaves wiki/notes/raw alone).
