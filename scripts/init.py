#!/usr/bin/env python3
"""
Scaffold a new twitter-wiki knowledge base.

Usage:
    python3 scripts/init.py <path> [--no-obsidian] [--no-git] [--force]

Creates the directory tree, renders CLAUDE.md from the template, and copies
Obsidian + gitignore config. Idempotent unless --force is set: refuses to
overwrite an existing CLAUDE.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path

# Path to the skill installation. This script lives at <skill>/scripts/init.py,
# so the skill root is the parent of the scripts dir.
SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_ROOT / "templates"


def render_template(template: str, vars: dict[str, str]) -> str:
    """Tiny mustache-style {{var}} substitution. No conditionals, no loops."""
    out = template
    for key, value in vars.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def init_kb(
    kb_path: Path,
    *,
    obsidian: bool = True,
    git: bool = True,
    force: bool = False,
) -> None:
    kb_path = kb_path.expanduser().resolve()
    claude_md = kb_path / "CLAUDE.md"

    if claude_md.exists() and not force:
        print(
            f"error: {claude_md} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Directory tree
    dirs = [
        kb_path,
        kb_path / "raw" / "bookmarks",
        kb_path / "wiki" / "queries",
        kb_path / "notes",
        kb_path / ".twitter-wiki",
    ]
    if obsidian:
        dirs.append(kb_path / ".obsidian")
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # CLAUDE.md from template
    template_path = TEMPLATES / "CLAUDE.md.tmpl"
    if not template_path.exists():
        print(f"error: template not found at {template_path}", file=sys.stderr)
        sys.exit(3)
    template = template_path.read_text()
    rendered = render_template(
        template,
        {
            "kb_name": kb_path.name.replace("-", " ").replace("_", " ").title(),
            "kb_path": str(kb_path),
            "created_date": dt.date.today().isoformat(),
        },
    )
    claude_md.write_text(rendered)

    # .gitignore
    gitignore_src = TEMPLATES / "gitignore.tmpl"
    if gitignore_src.exists():
        (kb_path / ".gitignore").write_text(gitignore_src.read_text())

    # Obsidian config
    if obsidian:
        obs_src = TEMPLATES / "obsidian"
        obs_dst = kb_path / ".obsidian"
        for f in obs_src.glob("*.json"):
            shutil.copy2(f, obs_dst / f.name)

    # Stub wiki/index.md and wiki/log.md so the directory isn't empty and
    # so Claude has something to read on first session startup.
    today = dt.date.today().isoformat()
    (kb_path / "wiki" / "index.md").write_text(
        f"""---
title: "Knowledge Base Index"
type: index
created: {today}
updated: {today}
---

**TLDR:** Master catalog of all wiki pages. Empty until first `/kb-ingest`.

| Page | Type | Tags | TLDR |
|---|---|---|---|

(no pages yet — run `/kb-sync` then `/kb-ingest` to populate)
"""
    )
    (kb_path / "wiki" / "log.md").write_text(
        f"""---
title: "Activity Log"
type: log
created: {today}
updated: {today}
---

**TLDR:** Chronological record of all knowledge base operations, newest first.

## {today}

- **init** — KB scaffolded at `{kb_path}`
"""
    )

    # README in notes/ explaining the boundary
    (kb_path / "notes" / "README.md").write_text(
        """# notes/

This directory is **yours**. Personal notes, drafts, journals, anything.

The twitter-wiki skill (and Claude in general) **never reads or writes** in
here. It's safe space.

If you want Claude to see something, put it in the conversation directly.
"""
    )

    # Git init
    if git:
        try:
            subprocess.run(
                ["git", "init", "-q", str(kb_path)],
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(
                f"warning: git init failed ({e}); skipping. KB is still usable.",
                file=sys.stderr,
            )

    # Friendly summary
    print(f"✓ KB scaffolded at {kb_path}")
    print()
    print("Next steps:")
    print(f"  cd {kb_path}")
    print("  claude")
    print("  /kb-sync       # pull bookmarks from your browser")
    print("  /kb-ingest     # bootstrap topics + build the wiki")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scaffold a new twitter-wiki KB."
    )
    parser.add_argument("path", type=Path, help="Where to create the KB")
    parser.add_argument(
        "--no-obsidian",
        action="store_true",
        help="Skip writing .obsidian/ config",
    )
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="Skip running git init",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing CLAUDE.md",
    )
    args = parser.parse_args()

    init_kb(
        args.path,
        obsidian=not args.no_obsidian,
        git=not args.no_git,
        force=args.force,
    )


if __name__ == "__main__":
    main()
