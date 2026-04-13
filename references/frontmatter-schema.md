# Frontmatter schema

Loaded during `/kb-ingest` and `/kb-lint`. Every wiki page starts with a
YAML frontmatter block. Missing or malformed frontmatter is a lint
error.

## Fields

```yaml
---
title: Human readable title
type: concept | person | event | resource | query | stale | index | log
sources: [list of @handles or URLs]
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [list of kebab-case tags]
---
```

### Required on every page

- **title** (string) — Human-readable, title case. Not kebab-case —
  that's the filename. Example: `title: LLM Agent Architectures`.
- **type** (enum) — see below.
- **created** (ISO date, `YYYY-MM-DD`) — the day the page was first
  written. Never modify after creation.
- **updated** (ISO date, `YYYY-MM-DD`) — the day of the most recent
  meaningful edit. Bump on every ingest that changes the body.
- **tags** (list of strings) — kebab-case, no `#`. 2–6 tags is typical.
  Tags are free-form and emerge from content; there is no fixed
  taxonomy. Reuse existing tags when they fit.

### Required on most pages

- **sources** (list) — the handles or URLs the page draws from. A
  handle looks like `@username` (keep the @). A URL is a full
  `https://...`. Required on any page synthesized from bookmarks.
  Optional on `type: query` if the answer didn't cite specific
  sources, and on `type: stale`.

## `type` values

| type | What it means | Counter-args required? |
|---|---|---|
| `concept` | A topic, idea, or claim synthesized from multiple sources | **Yes** |
| `person` | A profile of one author (their themes, stances, notable posts) | No |
| `event` | A time-bounded thing — a launch, an outage, a news cycle | No |
| `resource` | A curated list of links, tools, or reading | No |
| `query` | Saved answer to a `/kb-query` question | No |
| `stale` | Page no longer tracked after a recluster. Preserved for history | No |
| `index` | The `wiki/index.md` catalog. Exempt from TLDR/sources/tags/counter-args | No |
| `log` | The `wiki/log.md` audit trail. Exempt from TLDR/sources/tags/counter-args | No |

If a page doesn't clearly fit, use `concept`.

## Filename vs title

- **Filename** is kebab-case and matches the topic slug from
  `cluster-map.json`: `llm-agents.md`, not `LLMAgents.md`.
- **Title** in frontmatter is human-readable: `title: LLM Agents`.

## Examples

A concept page:

```yaml
---
title: Prompt Injection
type: concept
sources: ["@simonw", "@karpathy", "https://simonwillison.net/2023/prompt-injection"]
created: 2026-03-01
updated: 2026-04-12
tags: [security, llm, prompting]
---
```

A query page:

```yaml
---
title: What do people say about Rust async runtimes?
type: query
sources: ["@withoutboats", "@tokio_rs"]
created: 2026-04-13
updated: 2026-04-13
tags: [rust, async]
---
```

A stale page (kept after a recluster collapsed the topic):

```yaml
---
title: Web3 Infrastructure
type: stale
created: 2026-01-10
updated: 2026-04-13
tags: [archive]
---
```

## Lint enforcement

`scripts/lint.py` checks:

- Frontmatter block exists and parses as YAML.
- All required fields present for the declared `type`.
- `created` and `updated` are valid ISO dates; `updated >= created`.
- `type` is one of the allowed values.
- `tags` are all kebab-case.
- Pages with `type: concept` have a `## Counter-arguments` (or
  `## Counterarguments`) section in the body.

Fix violations autonomously when the fix is mechanical (reformat a
date, re-case a tag). Report to the user when the fix requires content
judgment (write a missing counter-arguments section from scratch).
