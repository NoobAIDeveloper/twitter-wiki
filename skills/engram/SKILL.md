---
name: engram
description: Manage a personal knowledge base built from a user's Twitter/X bookmarks, ChatGPT and Claude.ai conversation history, Claude Code sessions, Notion pages, Granola meeting notes, browser bookmarks, GitHub stars, and Kindle highlights. Sync from logged-in browser sessions, APIs, and local app caches, cluster by topic, and synthesize an interlinked Obsidian wiki. Activate when the user is in an engram KB directory (one with a CLAUDE.md that names this skill, or with a `.engram/` subdirectory) or asks to sync, ingest, query, lint, recluster, or otherwise work with their wiki.
---

# engram

You are operating a personal knowledge base built from a user's Twitter/X bookmarks. Bookmarks are stored as JSONL on disk. A deterministic Python script clusters them into topic batches using a YAML map that **you** generate from the user's actual content. You then synthesize each batch into a wiki page (frontmatter + TLDR + body + counter-arguments + wikilinks) inside an Obsidian vault.

The scripts handle the mechanical work (auth, fetching, clustering, lint checks, stats). You handle judgment: classifying topics, synthesizing insights, writing prose, fixing semantic issues.

## Plugin setup

This skill ships as a Claude Code plugin. On first use, ensure the plugin is linked and the Python venv exists:

```bash
# Link the plugin (one-time — skip if ~/.claude/skills/engram already exists)
if [ ! -e "$HOME/.claude/skills/engram" ]; then
  # Prefer CLAUDE_PLUGIN_ROOT (set by Claude Code when a plugin skill runs).
  INSTALL_PATH="${CLAUDE_PLUGIN_ROOT:-}"
  if [ -z "$INSTALL_PATH" ]; then
    INSTALL_PATH=$(python3 - <<'PY' 2>/dev/null
import json, pathlib
data = json.loads(pathlib.Path('~/.claude/plugins/installed_plugins.json').expanduser().read_text())
for pid, recs in data.get('plugins', {}).items():
    if 'engram' in pid and recs:
        print(recs[0]['installPath']); break
PY
)
  fi
  if [ -n "$INSTALL_PATH" ] && [ -d "$INSTALL_PATH" ]; then
    mkdir -p ~/.claude/skills
    ln -sf "$INSTALL_PATH" ~/.claude/skills/engram
  fi
fi
# Bootstrap Python venv (one-time)
if [ ! -d "$HOME/.claude/skills/engram/.venv" ]; then
  cd ~/.claude/skills/engram
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q "cryptography>=42"
fi
```

After this, all scripts are accessible via `~/.claude/skills/engram/`.

## Where things live

The user's KB directory:
- `raw/bookmarks.jsonl` — canonical bookmark store, one JSON per line. **Read-only for you.** Only `sync.py` writes here.
- `raw/bookmarks/<topic>.md` — topic batches derived from `cluster-map.json`. **Do not hand-edit.** Re-run preprocess to regenerate.
- `raw/bookmarks/_manifest.md` — index of batches with counts. Generated.
- `wiki/` — **yours.** Synthesized markdown pages. You write and rewrite freely.
- `wiki/index.md` — markdown table catalog of all wiki pages. You maintain it.
- `wiki/log.md` — chronological audit log, newest first. You append to it.
- `wiki/queries/` — saved query-result pages (created by `/kb-query` when answers are worth keeping).
- `notes/` — **user-only.** You NEVER read this directory. You NEVER write to it.
- `CLAUDE.md` — KB-level config & rules. Loaded automatically.
- `.engram/cluster-map.json` — topic → match rules. **You generate this on first ingest.**
- `.engram/sync-meta.json` — sync state. Owned by `sync.py`. Don't hand-edit.
- `.engram/ingest-state.json` — tracks which clusters have been synthesized. Owned by you (you update it during ingest).

The skill itself (read-only reference material):
- `~/.claude/skills/engram/scripts/*.py` — bundled Python scripts. Invoke via `~/.claude/skills/engram/.venv/bin/python <script> --kb $(pwd)` (the venv ships with required deps).
- `~/.claude/skills/engram/references/*.md` — verbose specs you load on demand via `@references/...`
- `~/.claude/skills/engram/templates/CLAUDE.md.tmpl` — KB template used by init

## Operations

These slash commands live in `~/.claude/commands/kb-*.md` and each one delegates to a workflow below. The user can also ask in natural language ("sync my bookmarks", "build the wiki") and you should run the matching workflow.

| Command | One-liner |
|---|---|
| `/kb-init <path>` | Scaffold a new KB at `<path>` |
| `/kb-sync` | Pull new bookmarks from the user's browser session |
| `/kb-ingest` | Cluster + synthesize wiki pages from current bookmarks |
| `/kb-recluster [hint]` | Re-derive the topic map and regenerate affected pages |
| `/kb-query <question>` | Answer a question grounded in the wiki |
| `/kb-lint` | Run lint script and fix what you can |
| `/kb-status` | Show sync + ingest stats |

## Ingest workflow

The most important workflow. Never deviate from this order.

1. **Bootstrap check.** If `.engram/cluster-map.json` does NOT exist:
   - Load `@~/.claude/skills/engram/references/clustering-guide.md`.
   - Read a **diversified sample** of `raw/items.jsonl` (or `raw/bookmarks.jsonl` on legacy KBs). Take 15% of each source, clamped to [10, 200] per source. This ensures multi-source KBs get topic coverage from every source, not just the dominant one. See `@~/.claude/skills/engram/references/clustering-guide.md` for diversification rules within each source.
   - Derive **8–20 topics** that fit THIS user's actual content. Topics emerge from what's bookmarked — never from a generic preset. If the user bookmarks recipes, cooking topics; if they bookmark trades, finance topics; etc.
   - Write `.engram/cluster-map.json`. Each topic entry: `name`, `description` (one line), `match` (keywords / hashtags / author handles / regex). See `@~/.claude/skills/engram/references/clustering-guide.md` for format.
   - Briefly tell the user what topics you derived and why. Offer them a chance to tweak before proceeding (one round of edits, then move on).

2. **Run preprocess.** `~/.claude/skills/engram/.venv/bin/python ~/.claude/skills/engram/scripts/preprocess.py --kb $(pwd)`. This writes `raw/bookmarks/<topic>.md` files and `_manifest.md`. Bookmarks matching nothing land in `_unsorted.md`.

3. **Read existing state.**
   - Read `wiki/index.md` (or note it doesn't exist yet).
   - Read `.engram/ingest-state.json` to know which batches were already synthesized at which sizes.

4. **For each topic batch in `raw/bookmarks/`:**
   - Skip `_manifest.md` and `_unsorted.md` (the unsorted file is for your awareness, not synthesis).
   - If the batch is new OR has grown since last ingest, synthesize/update `wiki/<topic>.md`.
   - Follow `@~/.claude/skills/engram/references/extraction-rules.md` for the synthesis approach.
   - Required structure (see `@~/.claude/skills/engram/references/frontmatter-schema.md`):
     - YAML frontmatter (title, type, sources, created, updated, tags)
     - **TLDR** section (3–5 sentence high-density summary)
     - Body grouped by sub-theme, with author attributions and short quotes for high-engagement tweets (>1000 likes)
     - **Counter-arguments** section (mandatory if `type: concept`)
     - Wikilinks `[[other-page]]` to other wiki pages, even if those pages don't exist yet (stub pages are OK)

5. **Regenerate `wiki/index.md`** as a markdown table: `| Page | Type | Tags | TLDR (one-liner) |`. Sort by category if there's a natural grouping; alphabetical otherwise.

6. **Append to `wiki/log.md`** under today's date: an `ingest-batch` entry per processed topic with bookmark count.

7. **Update `.engram/ingest-state.json`** with the sizes you just synthesized at.

8. **Report back to the user**: what was created/updated, what's still pending, any anomalies.

## Recluster workflow

When the user runs `/kb-recluster` (optionally with a natural-language hint like "merge two topics" or "split out finance from business"):

1. Back up the existing map: `cp .engram/cluster-map.json .engram/cluster-map.json.bak`.
2. Read the current `cluster-map.json`, current `_manifest.md`, and `wiki/index.md`.
3. Re-sample bookmarks (the corpus is likely much larger than at bootstrap time). Same diversity rules.
4. Apply the user's hint if any. Otherwise look for: topics that grew much larger than others (split candidates), topics that became near-duplicates (merge candidates), topics with <5 bookmarks (collapse candidates), unsorted bookmarks that suggest a missing topic.
5. Write the new `cluster-map.json`. Briefly explain the diff to the user.
6. Run preprocess.
7. Reconcile wiki pages:
   - **Renamed topic** → rename the wiki file, update wikilinks, note in log.
   - **Merged topics** → combine the wiki pages (preserve all content, dedupe), delete the orphan, redirect wikilinks.
   - **Split topic** → re-synthesize as multiple pages from the new batches.
   - **Removed topic** → mark the old wiki page with `type: stale` in frontmatter; do not delete (user may still want it).
8. Append to `wiki/log.md` under `recluster`.

## Query workflow

When the user asks a question via `/kb-query` (or natural language):

1. Read `wiki/index.md` to find candidate pages.
2. Read the relevant pages (don't read everything).
3. Answer grounded in what's in the wiki. Cite page names with wikilinks.
4. If the answer is novel and worth keeping (the user asked something the wiki doesn't already answer well, AND the answer is substantive), save it to `wiki/queries/<kebab-case-question>.md` with `type: query` frontmatter, the question, the answer, and the sources.
5. Append a `query` entry to `wiki/log.md`.

## Lint workflow

1. Run `~/.claude/skills/engram/.venv/bin/python ~/.claude/skills/engram/scripts/lint.py --kb $(pwd)`. It returns a structured report of issues (missing frontmatter, missing TLDR, missing counter-args on concept pages, broken wikilinks, orphan pages, stale pages).
2. For each issue, decide: can you fix it autonomously? If yes, fix it. If no (e.g. requires content judgment beyond what's in the existing pages), report it to the user.
3. Append to `wiki/log.md` under `lint`.

## Supported sources

`sync.py` dispatches to per-source adapters. Use `--source <name>` (or `--source all` to run everything configured).

| Source | Config | Notes |
|---|---|---|
| `x` | none | Browser cookies from chrome/brave/edge. The original source. |
| `chatgpt` | none | Browser cookies from chatgpt.com. Q+A pairs. Fallback: `/kb-request-chatgpt-export` + `/kb-import-chatgpt <zip>`. |
| `claude-ai` | none | Browser cookies from claude.ai. Q+A pairs. Fallback: manual export + `/kb-import-claude <zip>`. |
| `claude-code` | none | Walks `~/.claude/projects/` session logs. |
| `browser-bookmarks` | none | Reads Chrome/Brave/Edge bookmark JSON. |
| `github-stars` | handle in `sources.json` | Public API; `GITHUB_TOKEN` env var optional for higher rate limit. |
| `kindle` | `--clippings <path>` | One-shot import from `My Clippings.txt`. |

## Sync workflow

1. Run `~/.claude/skills/engram/.venv/bin/python ~/.claude/skills/engram/scripts/sync.py --kb $(pwd) [--source <name>]`. Default source is `x`. Source adapters handle their own cookie extraction, pagination, and dedupe; outputs land in `raw/items.jsonl` (or `raw/bookmarks.jsonl` for `x`) with `.engram/<source>-sync-meta.json` cursors.
2. Report new item count.
3. If new items were added, suggest `/kb-ingest`.
4. If sync failed with auth error, tell the user to re-login in the browser for that source.

## Hard invariants — never break these

- **`wiki/` is yours, `notes/` is the user's.** Never read `notes/`. Never write to `notes/`. This is non-negotiable.
- **Every wiki page** has YAML frontmatter, a TLDR section, and (if `type: concept`) a counter-arguments section. No exceptions.
- **Filenames are kebab-case.** No spaces, no underscores, no capitals.
- **Wikilinks use `[[double-brackets]]`.** External links use markdown `[text](url)`.
- **Topics emerge from the user's actual bookmarks.** Never assume a domain. Never hardcode topic categories. Re-derive from content every time.
- **Preprocess never runs without `cluster-map.json`.** If it's missing, bootstrap it first by reading bookmarks and writing the map.
- **Sync state files in `.engram/` are owned by scripts.** Don't hand-edit `sync-meta.json`. You may edit `cluster-map.json` (you generated it) and `ingest-state.json` (you maintain it).
- **High-engagement tweets get direct quotes.** If a tweet has >1000 likes, include a short verbatim quote (with attribution) in the wiki page rather than just paraphrasing.
- **Don't ingest into a wiki page if the source batch hasn't changed.** Check `ingest-state.json` first. Avoid redundant LLM work.

## Backward compatibility

If you encounter a KB with a `.twitter-wiki/` directory (the old name), migrate it:
```bash
mv .twitter-wiki .engram
```
Then proceed normally. This is a one-time migration — the old name is no longer used.

## On context efficiency

This SKILL.md is loaded into your context whenever you operate on an engram KB. Detailed schemas, taxonomies, and rule sets live in `references/*.md` and are loaded only when you actually need them. When you're doing a small operation like `/kb-status`, do NOT pull in the references — they're not needed. When you're doing `/kb-ingest`, load `extraction-rules.md`, `frontmatter-schema.md`, and (on bootstrap) `clustering-guide.md`, and nothing else.
