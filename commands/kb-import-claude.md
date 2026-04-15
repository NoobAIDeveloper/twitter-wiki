---
description: Import a Claude.ai export zip into this KB (fallback when /kb-sync --source claude-ai breaks)
argument-hint: <path-to-export.zip>
---

The user wants to import Claude.ai conversations from an official export zip. This is the fallback path when the live cookie-based sync breaks.

How the user gets the zip: **claude.ai → Settings → Privacy → Export data**. They get an email with a download link.

The current working directory should be a KB (contain `CLAUDE.md`). If not, tell them to `cd` into their KB first or run `/kb-init`.

`$ARGUMENTS` is the path to the zip. If missing, ask the user for it.

Run:

```bash
~/.claude/skills/twitter-wiki/.venv/bin/python ~/.claude/skills/twitter-wiki/scripts/import_export.py \
  --kb $(pwd) --provider claude-ai --zip $ARGUMENTS
```

After it finishes:

- **Success:** report the number of Q+A pairs imported and suggest `/kb-ingest` to weave them into the wiki.
- **"does not contain conversations.json":** they gave a wrong zip — tell them to look in the email link from Anthropic.
- **FileNotFound:** the path is wrong.

Don't auto-run `/kb-ingest`.
