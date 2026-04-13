# Clustering guide

Loaded on first ingest and on `/kb-recluster`. The goal is to produce
`.twitter-wiki/cluster-map.json` — a deterministic routing map that
`preprocess.py` applies to the bookmarks.

Topics emerge from **this user's actual bookmarks**. Never from a preset.
If the user bookmarks cooking, derive cooking topics. If they bookmark
finance, derive finance topics. Don't assume a domain.

## Sampling

Before deriving topics, sample `raw/bookmarks.jsonl` with diversity, not
just the top N lines:

- Aim for ~80 bookmarks total (cap at ~120 for very large corpora).
- Spread across the full time range (`postedAt`): include oldest and
  newest, not just recent.
- Spread across distinct `authorHandle` values — don't let one prolific
  author dominate.
- Spread across distinct hashtags when present.
- If the corpus is <200 bookmarks, just read all of them.

Read the bookmark `text`, `authorHandle`, and hashtags. You do not need
engagement numbers for clustering.

## Deriving topics

- Produce **8–20 topics**. Fewer than 8 and batches get too broad to
  synthesize well. More than 20 and you're over-fitting.
- Each topic should cover at least ~5 bookmarks in the current corpus
  (check against your sample). Singletons belong in `_unsorted` until
  they accumulate.
- Topics should be **orthogonal where possible** but multi-assign is
  allowed — a tweet about "LLM evals in finance" can legitimately land
  in both a models topic and a finance topic.
- Topic names are **kebab-case** (`llm-agents`, not `LLM Agents` or
  `llm_agents`). No spaces, no capitals, no underscores.
- Descriptions are one line — they show up in `_manifest.md` and help
  future-you remember what the topic is for.

## cluster-map.json format

```json
{
  "version": 1,
  "topics": [
    {
      "name": "topic-slug",
      "description": "One-line description of what belongs here.",
      "match": {
        "keywords": ["case-insensitive substrings matched against tweet text"],
        "hashtags": ["hashtags without the #, case-insensitive"],
        "authors":  ["handles without the @, case-insensitive"],
        "regex":    ["Python regex patterns, case-insensitive"]
      }
    }
  ]
}
```

All four `match` fields are optional; include only what you need. A
bookmark matches the topic if **any** rule hits (OR semantics across
rule types, OR across entries within a type).

### Choosing rules

- **keywords** are the workhorse. Prefer specific multi-word phrases
  (`"prompt injection"`) over single generic words (`"ai"`) that will
  over-match.
- **hashtags** are useful when the user's community tags reliably
  (`#rustlang`, `#buildinpublic`). Skip if hashtags are rare.
- **authors** pin a handle to a topic when that person posts almost
  exclusively about one thing. Don't pin broad-interest accounts.
- **regex** is escape hatch for patterns keywords can't express. Keep
  them readable. Always case-insensitive (the applier adds the flag).

### Example (domain-neutral — do not copy wholesale)

```json
{
  "version": 1,
  "topics": [
    {
      "name": "home-cooking",
      "description": "Recipes, techniques, and kitchen tips for home cooks.",
      "match": {
        "keywords": ["recipe", "sourdough", "braise", "weeknight dinner"],
        "hashtags": ["cooking", "recipes"],
        "authors":  ["kenjilopezalt"]
      }
    },
    {
      "name": "personal-finance",
      "description": "Saving, investing, taxes, and household money decisions.",
      "match": {
        "keywords": ["index fund", "401k", "roth", "emergency fund"],
        "regex":    ["\\$[0-9]+[km]?\\s+saved"]
      }
    }
  ]
}
```

## When to recluster

The user invokes `/kb-recluster`. Look for:

- A topic that grew much larger than peers → **split candidate**.
- Two topics with heavy overlap in their pages → **merge candidate**.
- A topic with <5 bookmarks after the corpus has grown → **collapse
  candidate** (fold into a broader topic or remove).
- `_unsorted.md` is large or reveals a missing topic → **add a topic**.

Back up `cluster-map.json` before rewriting (the skill does this via
`cp ... .bak`). Explain the diff to the user before re-running
preprocess.
