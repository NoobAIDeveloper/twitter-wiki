#!/usr/bin/env bash
# Install the twitter-wiki Claude Code skill.
#
# Creates:
#   ~/.claude/skills/twitter-wiki     symlink to this repo
#   ~/.claude/commands/kb-*.md        symlinks to commands/kb-*.md
#   <repo>/.venv/                     Python venv with cryptography installed
#
# All dependencies are handled automatically. The only real requirement is
# Python 3.10+ (macOS 12+ ships with it; most Linux distros do too).
#
# Idempotent: safe to re-run. Use ./install.sh --uninstall to remove.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
SKILL_LINK="$HOME/.claude/skills/twitter-wiki"
COMMANDS_DIR="$HOME/.claude/commands"

uninstall() {
  echo "Removing twitter-wiki skill..."
  [ -L "$SKILL_LINK" ] && rm "$SKILL_LINK" && echo "  removed $SKILL_LINK"
  for cmd in "$REPO_DIR"/commands/kb-*.md; do
    name="$(basename "$cmd")"
    target="$COMMANDS_DIR/$name"
    if [ -L "$target" ]; then
      rm "$target"
      echo "  removed $target"
    fi
  done
  echo "Note: the .venv directory inside the repo is left in place."
  echo "      delete it manually if you want to fully uninstall."
  exit 0
}

[ "${1:-}" = "--uninstall" ] && uninstall

# --- Python check ------------------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
  cat >&2 <<EOF
error: python3 is not installed.

  macOS: installed by default on macOS 12+. If missing, run:
           xcode-select --install
         or install from https://www.python.org/downloads/

  Linux: sudo apt install python3 python3-venv   (Debian/Ubuntu)
         sudo dnf install python3                (Fedora)
EOF
  exit 1
fi

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)')"
if [ "$PY_OK" != "1" ]; then
  echo "error: Python 3.10+ required (found $PY_VER)" >&2
  exit 1
fi

# --- venv + deps -------------------------------------------------------------

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating Python venv at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi

echo "Installing Python dependencies (cryptography) ..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet "cryptography>=42"

# --- symlinks ----------------------------------------------------------------

mkdir -p "$HOME/.claude/skills" "$COMMANDS_DIR"

if [ -L "$SKILL_LINK" ] && [ "$(readlink "$SKILL_LINK")" = "$REPO_DIR" ]; then
  echo "skill already linked"
elif [ -e "$SKILL_LINK" ] || [ -L "$SKILL_LINK" ]; then
  echo "error: $SKILL_LINK exists and does not point to $REPO_DIR" >&2
  echo "  remove it and re-run: rm $SKILL_LINK && ./install.sh" >&2
  exit 1
else
  ln -s "$REPO_DIR" "$SKILL_LINK"
  echo "linked skill  -> $SKILL_LINK"
fi

for cmd in "$REPO_DIR"/commands/kb-*.md; do
  name="$(basename "$cmd")"
  target="$COMMANDS_DIR/$name"
  if [ -L "$target" ] && [ "$(readlink "$target")" = "$cmd" ]; then
    continue
  fi
  if [ -e "$target" ]; then
    echo "  skipping $name (existing file is not our symlink)" >&2
    continue
  fi
  ln -s "$cmd" "$target"
  echo "linked command -> $target"
done

cat <<EOF

---------------------------------------------------------------
twitter-wiki installed.

Next steps:

  1. Open Claude Code in any directory:  claude
  2. Scaffold a KB:                      /kb-init ~/my-kb
  3. Open Claude Code inside it:         cd ~/my-kb && claude
  4. Pull your bookmarks:                /kb-sync
  5. Build the wiki:                     /kb-ingest

Uninstall: ./install.sh --uninstall
---------------------------------------------------------------
EOF
