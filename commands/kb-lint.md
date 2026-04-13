---
description: Lint the wiki and fix what can be fixed autonomously
---

The user wants to lint the wiki. The current working directory should be a twitter-wiki KB (it should contain a `CLAUDE.md` and a `.twitter-wiki/` subdirectory). If it doesn't, tell the user to `cd` into their KB first or run `/kb-init` to scaffold one.

Run the lint script:

```bash
~/.claude/skills/twitter-wiki/.venv/bin/python ~/.claude/skills/twitter-wiki/scripts/lint.py --kb $(pwd)
```

The script emits a structured report of issues (missing frontmatter, missing TLDR, missing counter-args on concept pages, broken wikilinks, orphan pages, stale pages). Follow the **Lint workflow** in SKILL.md: for each issue, fix it autonomously if you can, or surface it to the user if it needs content judgment beyond what's already on the page. Append a `lint` entry to `wiki/log.md` when done.

Report back with two lists: what you fixed and what still needs the user's attention. If the script itself fails (non-zero exit, not a findings report), surface the error verbatim and stop — don't try to hand-roll the checks.
