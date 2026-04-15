---
description: Ask OpenAI to email you a ChatGPT data export (escape hatch when live sync is CF-blocked)
---

The user wants OpenAI to email them a download link for a full ChatGPT data export. This is the Option B escape hatch for when `/kb-sync --source chatgpt` fails with a Cloudflare block or stale auth the user can't clear.

We POST to `/backend-api/accounts/data_export` using their browser session cookie. OpenAI then sends an email with a download link (usually minutes, sometimes hours). When it arrives, they run `/kb-import-chatgpt <path-to-zip>`.

This command doesn't require a KB — it just triggers the email. Don't `cd` checks.

Run:

```bash
~/.claude/skills/twitter-wiki/.venv/bin/python ~/.claude/skills/twitter-wiki/scripts/sources/chatgpt.py \
  --request-export
```

If OpenAI's reauth check kicks in (it usually does on first run), the script will auto-open chatgpt.com → Data Controls in the user's default browser and wait at an `input()` prompt. Tell the user to re-enter their password in the browser tab, then return to the terminal and press Enter — the script retries automatically. Non-TTY sessions print a re-run instruction instead.

After it finishes:

- **Success:** tell the user to watch their inbox, download the zip when it arrives, and run `/kb-import-chatgpt <path-to-zip>`.
- **Non-TTY ("Re-authenticate in the browser tab..."):** the browser tab is already open; tell them to re-enter their password there, then re-run this command.
- **Cloudflare block:** this endpoint uses the same cookies as live sync, so if CF is blocking everything the trigger will also fail. Direct them to **chatgpt.com → Settings → Data Controls → Export data** in their browser.

Don't auto-run `/kb-import-chatgpt` — the email takes time, the user will come back.
