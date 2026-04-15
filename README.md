# twitter-wiki

Turn your Twitter/X bookmarks, ChatGPT and Claude.ai conversations, Claude Code
sessions, browser bookmarks, GitHub stars, and Kindle highlights into a living,
interlinked knowledge base that you can actually read. A Claude Code skill
plus a small set of Python scripts.

You bookmark things, chat with AI assistants, and star repos intending to come
back to them. You don't. This tool syncs all of it to your machine, clusters
the content into topics derived from *your* material, and asks Claude to
synthesize each cluster into an Obsidian wiki page — TLDR, inline author
attribution, direct quotes for high-engagement content, counter-arguments,
wikilinks to related pages.

It works for any domain. No hardcoded topic list. If you bookmark recipes you
get cooking pages; if you bookmark trades you get finance pages; if both, you
get both.

---

## How it works

```
X bookmarks  →  sync.py        →  raw/bookmarks.jsonl
                 (reads your browser cookies, hits X's internal GraphQL)

bookmarks    →  Claude         →  .twitter-wiki/cluster-map.json
                 (samples your content, derives 8–20 topics)

cluster map  →  preprocess.py  →  raw/bookmarks/<topic>.md
                 (deterministic router, multi-assign)

batches      →  Claude         →  wiki/<topic>.md
                 (synthesis: TLDR + prose + counter-args + wikilinks)
```

Claude handles judgment (clustering, synthesis, prose). The scripts handle
mechanical work (auth, fetching, routing, linting). The `wiki/` directory is
Claude's. The `notes/` directory is yours — the skill never reads it.

---

## Requirements

- macOS or Linux (Windows not supported in v1 — cookie decryption uses DPAPI there).
- Python 3.10+.
- [Claude Code](https://claude.com/claude-code) installed and working.
- A browser (Chrome, Brave, or Edge) logged into x.com.
- Optional: [Obsidian](https://obsidian.md) to read the wiki. Plain markdown works too.

---

## Install

```bash
git clone https://github.com/<you>/twitter-wiki ~/src/twitter-wiki
cd ~/src/twitter-wiki
./install.sh
```

This symlinks the skill into `~/.claude/skills/twitter-wiki` and the slash
commands into `~/.claude/commands/`. Edits to the repo are live — no rebuild.

Uninstall: `./install.sh --uninstall`.

---

## Quickstart

```bash
# 1. Scaffold a KB. Do this from any shell.
claude
> /kb-init ~/my-kb

# 2. Start a fresh Claude session inside the KB so CLAUDE.md loads.
cd ~/my-kb
claude

# 3. Pull bookmarks from your browser.
> /kb-sync

# 4. Let Claude cluster + synthesize.
> /kb-ingest
# On first run, Claude samples your bookmarks, derives 8–20 topics, and shows
# you the cluster map before proceeding. You get one round of edits.

# 5. Ask questions.
> /kb-query what do people I follow say about prompt injection?

# 6. Health check.
> /kb-lint
```

Open `~/my-kb` in Obsidian for the full experience (wikilink graph, page
preview). Plain markdown readers work too.

---

## Data sources

| Source | Config | How it syncs |
|---|---|---|
| `x` | none | Browser cookies → X's internal GraphQL. Default source. |
| `chatgpt` | none | Browser cookies → ChatGPT's backend API. Q+A pairs extracted from conversation history. Fallback: `/kb-request-chatgpt-export` + `/kb-import-chatgpt <zip>`. |
| `claude-ai` | none | Browser cookies → Claude.ai's backend API. Fallback: manual export + `/kb-import-claude <zip>`. |
| `claude-code` | none | Reads local Claude Code session logs under `~/.claude/projects/`. |
| `browser-bookmarks` | none | Reads Chrome/Brave/Edge bookmark JSON. |
| `github-stars` | GitHub handle | Public API. `GITHUB_TOKEN` env var optional for higher rate limit. |
| `kindle` | `--clippings` path | One-shot import from `My Clippings.txt` on the Kindle drive. |

Run `/kb-sync --source <name>` or `/kb-sync --source all`. See `/kb-add-source <name>` for per-source config instructions.

---

## Slash commands

All commands are run inside a KB directory.

| Command | Purpose |
|---|---|
| `/kb-init <path>` | Scaffold a new KB (directory tree + CLAUDE.md + Obsidian config). |
| `/kb-sync` | Pull new bookmarks from your logged-in browser. Incremental. |
| `/kb-ingest` | Cluster + synthesize. Bootstraps the cluster map on first run. |
| `/kb-recluster [hint]` | Re-derive the topic map. Optional natural-language hint like `"split finance from business"`. |
| `/kb-query <question>` | Ask a question grounded in the wiki. Saves substantive answers. |
| `/kb-lint` | Check frontmatter, TLDRs, counter-args, wikilinks, orphans. |
| `/kb-status` | Sync state, bookmark count, ingest freshness. |

---

## What a KB looks like

```
my-kb/
├── CLAUDE.md                          # KB-level rules (generated)
├── .twitter-wiki/
│   ├── cluster-map.json               # Claude-generated topic → match rules
│   ├── sync-meta.json                 # Owned by sync.py
│   └── ingest-state.json              # Tracks what's been synthesized
├── raw/
│   ├── bookmarks.jsonl                # Canonical bookmark store
│   └── bookmarks/
│       ├── _manifest.md               # Counts per topic
│       ├── _unsorted.md               # Bookmarks matching no topic
│       └── <topic>.md                 # One file per topic
├── wiki/                              # Claude's output. Synthesized pages.
│   ├── index.md                       # Catalog table
│   ├── log.md                         # Chronological audit log
│   ├── queries/                       # Saved /kb-query answers
│   └── <topic>.md
├── notes/                             # Yours. The skill never reads this.
└── .obsidian/                         # Obsidian vault config
```

---

## How bookmark sync actually works

X has no public bookmark API. The skill does what your browser does:

1. Reads the encrypted cookie store from Chrome/Brave/Edge on disk.
2. Decrypts the `ct0` (CSRF) and `auth_token` cookies using the key stored in
   macOS Keychain (or GNOME Keyring on Linux).
3. Calls X's internal GraphQL `Bookmarks` endpoint with those cookies plus
   the standard public Bearer token the web client uses.
4. Paginates until it hits a bookmark already known locally (incremental) or
   runs out (full sync).

Implications:
- You need to be **logged into X in your browser** — the cookies are the auth.
- Fragile by design. If X rotates their GraphQL query ID or changes cookie
  encryption, sync breaks until the skill is updated.
- No tokens, no API keys, no developer account required.
- If you use multiple browsers, pass `--browser chrome|brave|edge` to pin one.

---

## How clustering actually works

There is **no built-in topic list**. On first `/kb-ingest`:

1. Claude samples ~80 bookmarks from your JSONL with diversity across time,
   authors, and hashtags.
2. Claude derives 8–20 kebab-case topics from what it sees. If your bookmarks
   are mostly recipes, you get cooking topics. If they're mostly trades,
   finance topics. The skill is domain-agnostic.
3. Claude writes `.twitter-wiki/cluster-map.json` — topic name, description,
   and match rules (keywords / hashtags / authors / regex). It's plain JSON
   so you can hand-edit it in any text editor.
4. You review. One round of edits, then proceed.
5. `preprocess.py` applies the map deterministically. **Multi-assign**: a
   bookmark can land in multiple batches (a tweet about "LLM evals in
   finance" lands in both an ML topic and a finance topic).

`/kb-recluster` re-runs the derivation when topics drift. You can pass a hint
like `"merge business and entrepreneurship"` and Claude will reconcile the
wiki pages — renames, merges, splits — preserving content and updating
wikilinks.

---

## Synthesis rules

Every synthesized wiki page has:

- **YAML frontmatter** — title, type, sources, created, updated, tags.
- **TLDR** — 3–5 dense sentences. The entire page collapsed.
- **Body** — grouped by sub-theme, inline author attribution.
- **Counter-arguments** — required on `type: concept` pages.
- **Wikilinks** — `[[kebab-case]]` to related pages (stubs OK, lint flags orphans).
- **Direct quotes** for tweets with >1000 likes (short, attributed).

Full spec: [`references/extraction-rules.md`](references/extraction-rules.md)
and [`references/frontmatter-schema.md`](references/frontmatter-schema.md).

---

## Privacy

- Everything runs locally. Bookmarks go from X → your browser's cookie jar →
  your disk. Nothing leaves your machine except the requests to `x.com` that
  the sync script makes on your behalf.
- Synthesis uses Claude via your existing Claude Code session, which talks to
  Anthropic's API under your account. The bookmarks themselves are sent to
  Claude as part of that synthesis context. If that's a concern, don't ingest
  DMs or private content.
- The `notes/` directory is never read by the skill. Use it for anything you
  don't want Claude to see.

---

## Troubleshooting

**`/kb-sync` fails with an auth error.** Cookies are stale. Log out and back
into X in your browser, then retry. If the browser wasn't running recently,
the cookie database may be locked — close it.

**`/kb-sync` fails with "keychain dialog timed out".** On the first sync, macOS
pops a dialog asking permission for `security` to read the browser's Safe
Storage password. You now have 2 minutes to enter your password — but if you
miss it, just rerun `/kb-sync`. **Tip:** click **Always Allow** the first time
so you're never prompted again.

**`/kb-sync` says no browser found.** The skill looks for Chrome, Brave, and
Edge at their standard paths on macOS/Linux. Make sure you're logged in.

**Sync works but `/kb-ingest` never starts clustering.** Check that
`raw/bookmarks.jsonl` has content. On very small corpora (<20 bookmarks),
Claude may ask whether you really want to cluster yet — wait until you have
more.

**Lint errors on `wiki/index.md` or `wiki/log.md`.** These should pass as
`type: index` / `type: log`. If your scaffold predates the fix, regenerate
with `./install.sh && /kb-init ~/new-kb` or edit the types manually.

**X rotated their GraphQL.** The query ID is pinned in
`scripts/graphql.py`. When it breaks, the fix is updating that constant.
File an issue.

---

## Layout of this repo

```
twitter-wiki/
├── SKILL.md                       # The skill manifest — loaded into Claude's context
├── install.sh                     # Installer (symlinks into ~/.claude/)
├── commands/                      # Slash command definitions
│   └── kb-*.md
├── scripts/                       # Python scripts, invoked via the bundled .venv
│   ├── init.py                    # Scaffold a KB
│   ├── sync.py                    # Pull bookmarks from X
│   ├── cookies.py                 # Browser cookie extraction (internal)
│   ├── graphql.py                 # X GraphQL client (internal)
│   ├── preprocess.py              # Apply cluster map → topic batches
│   └── lint.py                    # KB health check
├── references/                    # Verbose specs Claude loads on-demand
│   ├── clustering-guide.md
│   ├── extraction-rules.md
│   └── frontmatter-schema.md
└── templates/                     # Files copied into new KBs
    ├── CLAUDE.md.tmpl
    ├── gitignore.tmpl
    └── obsidian/
```

---

## License

MIT. See [LICENSE](LICENSE).

Cookie extraction and GraphQL fetching techniques are adapted from
[fieldtheory-cli](https://github.com/afar1/fieldtheory-cli) (also MIT).
