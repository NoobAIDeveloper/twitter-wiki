---
description: Re-derive the topic map and reconcile affected wiki pages
argument-hint: [natural-language hint, e.g. "split finance from business"]
---

The user wants to rework the cluster map. The current working directory should be a twitter-wiki KB (it should contain a `CLAUDE.md` and a `.twitter-wiki/` subdirectory). If it doesn't, tell the user to `cd` into their KB first or run `/kb-init` to scaffold one.

Also confirm `.twitter-wiki/cluster-map.json` already exists. If it doesn't, there's nothing to recluster — tell the user to run `/kb-ingest` first (which bootstraps the map) and stop.

Treat `$ARGUMENTS` as an optional natural-language hint describing what the user wants changed (e.g. "merge crypto into finance", "split out cooking"). If empty, proceed with no hint and rely on the signals listed in the workflow.

Follow the **Recluster workflow** in SKILL.md end to end — back up the old map, re-sample, propose a new map, explain the diff to the user, run preprocess, then reconcile wiki pages (rename / merge / split / stale) and log it.

Before you overwrite the map, show the user the proposed diff and wait for confirmation. One round of edits, then commit. After reconciliation, report which wiki pages were renamed, merged, split, or marked stale.
