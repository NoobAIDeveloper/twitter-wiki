#!/usr/bin/env python3
"""
Lint a twitter-wiki KB.

Scans `<kb>/wiki/**/*.md` and reports issues: missing or malformed
frontmatter, bad `type` values, invalid dates, non-kebab-case tags or
filenames, missing TLDR sections, missing Counter-arguments on concept
pages, broken wikilinks, and orphan pages.

Frontmatter is YAML by convention but we parse a restricted subset
inline (flat key:value pairs, inline-list values, quoted or unquoted
scalars). This keeps the script stdlib-only.

Usage:
    python3 scripts/lint.py --kb <kb-path> [--json]

Exit code: 0 if clean, 1 if any issues were found.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Any


VALID_TYPES = {"concept", "person", "event", "resource", "query", "stale", "index", "log"}
META_TYPES = {"index", "log"}  # exempt from content checks (TLDR, sources, tags, counter-args)
KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
TLDR_RE = re.compile(r"^#+\s*TL;?DR\b", re.IGNORECASE | re.MULTILINE)
COUNTER_RE = re.compile(
    r"^#+\s*counter[\s-]?arguments?\b", re.IGNORECASE | re.MULTILINE
)


@dataclass
class Issue:
    code: str
    path: str
    message: str
    severity: str = "error"  # error | warn


# ---- checks per page --------------------------------------------------------

def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if not s:
        return ""
    # inline list
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        out: list[str] = []
        buf = ""
        quote = None
        for ch in inner:
            if quote:
                if ch == quote:
                    quote = None
                else:
                    buf += ch
            elif ch in ('"', "'"):
                quote = ch
            elif ch == ",":
                out.append(_strip_quotes(buf))
                buf = ""
            else:
                buf += ch
        if buf.strip():
            out.append(_strip_quotes(buf))
        return out
    return _strip_quotes(s)


def parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, str | None]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, text, "no frontmatter block"
    meta: dict[str, Any] = {}
    for i, line in enumerate(m.group(1).splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            return None, m.group(2), f"line {i}: expected `key: value`, got {stripped!r}"
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not key:
            return None, m.group(2), f"line {i}: empty key"
        meta[key] = _parse_scalar(value)
    return meta, m.group(2), None


def parse_iso_date(v: Any) -> date | None:
    if isinstance(v, date):
        return v
    if not isinstance(v, str):
        return None
    try:
        return date.fromisoformat(v.strip())
    except ValueError:
        return None


def lint_page(
    path: Path,
    rel: str,
    known_pages: set[str],
) -> tuple[list[Issue], set[str]]:
    """Return (issues, wikilink-targets-referenced)."""
    issues: list[Issue] = []
    refs: set[str] = set()

    # Filename
    stem = path.stem
    if not KEBAB_RE.match(stem):
        issues.append(Issue("bad-filename", rel,
            f"filename {stem!r} is not kebab-case"))

    text = path.read_text()
    meta, body, fm_err = parse_frontmatter(text)

    if fm_err or meta is None:
        issues.append(Issue("missing-frontmatter", rel, fm_err or "frontmatter missing"))
        # Still try to find wikilinks so we don't hide orphan data
        for m in WIKILINK_RE.finditer(text):
            refs.add(m.group(1).strip())
        return issues, refs

    page_type = meta.get("type")
    is_meta = page_type in META_TYPES

    # Required fields (tags not required on meta pages)
    required = ("title", "type", "created", "updated") if is_meta else \
               ("title", "type", "created", "updated", "tags")
    for field in required:
        if field not in meta:
            issues.append(Issue("missing-field", rel, f"missing frontmatter field {field!r}"))

    # type enum
    if page_type is not None and page_type not in VALID_TYPES:
        issues.append(Issue("bad-type", rel,
            f"type {page_type!r} not in {sorted(VALID_TYPES)}"))

    # sources required unless stale, query, or meta page
    if page_type not in {"stale", "query"} and not is_meta and not meta.get("sources"):
        issues.append(Issue("missing-sources", rel,
            f"type {page_type!r} requires a non-empty sources list"))

    # dates
    created = parse_iso_date(meta.get("created"))
    updated = parse_iso_date(meta.get("updated"))
    if "created" in meta and created is None:
        issues.append(Issue("bad-date", rel, f"created {meta.get('created')!r} is not ISO YYYY-MM-DD"))
    if "updated" in meta and updated is None:
        issues.append(Issue("bad-date", rel, f"updated {meta.get('updated')!r} is not ISO YYYY-MM-DD"))
    if created and updated and updated < created:
        issues.append(Issue("date-order", rel, "updated is earlier than created"))

    # tags
    tags = meta.get("tags")
    if tags is not None:
        if not isinstance(tags, list):
            issues.append(Issue("bad-tags", rel, "tags must be a list"))
        else:
            for t in tags:
                if not isinstance(t, str) or not KEBAB_RE.match(t):
                    issues.append(Issue("bad-tags", rel,
                        f"tag {t!r} is not kebab-case"))

    # Body checks (skip for stale — archival — and for meta pages)
    if page_type != "stale" and not is_meta:
        if not TLDR_RE.search(body):
            issues.append(Issue("missing-tldr", rel, "no TLDR section found"))
        if page_type == "concept" and not COUNTER_RE.search(body):
            issues.append(Issue("missing-counterargs", rel,
                "concept page has no Counter-arguments section"))

    # Wikilinks
    for m in WIKILINK_RE.finditer(body):
        target = m.group(1).strip()
        refs.add(target)
        if target not in known_pages:
            issues.append(Issue("broken-wikilink", rel,
                f"[[{target}]] does not resolve to a wiki page",
                severity="warn"))

    return issues, refs


# ---- main -------------------------------------------------------------------

def collect_wiki_pages(wiki_dir: Path) -> list[Path]:
    return sorted(p for p in wiki_dir.rglob("*.md") if p.is_file())


def main() -> int:
    ap = argparse.ArgumentParser(description="Lint a twitter-wiki KB.")
    ap.add_argument("--kb", required=True, type=Path)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()

    kb: Path = args.kb.resolve()
    wiki_dir = kb / "wiki"
    if not wiki_dir.exists():
        sys.exit(f"error: {wiki_dir} does not exist")

    pages = collect_wiki_pages(wiki_dir)
    known = {p.stem for p in pages}

    all_issues: list[Issue] = []
    all_refs: set[str] = set()
    for p in pages:
        rel = str(p.relative_to(kb))
        issues, refs = lint_page(p, rel, known)
        all_issues.extend(issues)
        all_refs.update(refs)

    # Orphan detection: pages never wikilinked from anywhere, excluding
    # the entry points (index, log) and query pages (query/ is a sink).
    for p in pages:
        if p.stem in {"index", "log"}:
            continue
        if p.parent.name == "queries":
            continue
        if p.stem not in all_refs:
            rel = str(p.relative_to(kb))
            all_issues.append(Issue("orphan-page", rel,
                "page is not linked from any other wiki page",
                severity="warn"))

    errors = [i for i in all_issues if i.severity == "error"]
    warns = [i for i in all_issues if i.severity == "warn"]

    if args.json:
        print(json.dumps({
            "kb": str(kb),
            "pages": len(pages),
            "errors": len(errors),
            "warnings": len(warns),
            "issues": [asdict(i) for i in all_issues],
        }, indent=2))
    else:
        if not all_issues:
            print(f"clean · {len(pages)} page(s) checked")
            return 0
        print(f"{len(pages)} page(s) checked · {len(errors)} error(s) · {len(warns)} warning(s)")
        for i in all_issues:
            marker = "ERR " if i.severity == "error" else "warn"
            print(f"  [{marker}] {i.code}  {i.path}: {i.message}")

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
