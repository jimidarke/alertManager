#!/usr/bin/env bash
#
# install.sh -- install (or update) the alert-manager Claude Code skill
# into ~/.claude/skills/alert-manager (personal, system-wide scope), or
# any directory set via CLAUDE_SKILLS_DIR (e.g. a repo's own
# .claude/skills, for project scope instead).
#
# Two ways to run this:
#
#   1. From a local checkout of this repo:
#        bash skills/alert-manager/install.sh
#      Copies straight from the checkout -- no network trip needed.
#
#   2. Standalone, no checkout required:
#        curl -fsSL https://raw.githubusercontent.com/jimidarke/alertManager/main/skills/alert-manager/install.sh | bash
#      Does a shallow, sparse git clone of just this directory (not the
#      whole repo -- Docker images, logs, etc. aren't needed for this),
#      copies from there, and cleans up.
#
# Re-run either form later to update: this script is idempotent and
# always overwrites the destination with the current source. There is no
# separate "update" command and no background auto-updater -- re-running
# this exact command is the whole update story, on purpose (see the
# skill's own README.md for why).

set -euo pipefail

REPO_URL="https://github.com/jimidarke/alertManager.git"
DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/alert-manager"

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR=""
if [[ -f "$SCRIPT_PATH" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
fi

TMP_CLONE=""
cleanup() { [[ -n "$TMP_CLONE" ]] && rm -rf "$TMP_CLONE"; }
trap cleanup EXIT

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/SKILL.md" ]]; then
  SRC="$SCRIPT_DIR"
  echo "Installing from local checkout: $SRC"
else
  command -v git >/dev/null 2>&1 || {
    echo "error: git is required to install without a local checkout of this repo" >&2
    exit 1
  }
  TMP_CLONE="$(mktemp -d)"
  echo "No local checkout found next to this script -- fetching skills/alert-manager from $REPO_URL ..."
  git clone --quiet --filter=blob:none --sparse --depth 1 "$REPO_URL" "$TMP_CLONE/repo"
  git -C "$TMP_CLONE/repo" sparse-checkout set skills/alert-manager >/dev/null
  SRC="$TMP_CLONE/repo/skills/alert-manager"
fi

if [[ ! -f "$SRC/SKILL.md" ]]; then
  echo "error: $SRC does not contain SKILL.md -- fetch/checkout is incomplete, aborting without touching $DEST" >&2
  exit 1
fi

mkdir -p "$DEST"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete "$SRC"/ "$DEST"/
else
  rm -rf "${DEST:?}"/*
  cp -r "$SRC"/. "$DEST"/
fi
chmod +x "$DEST"/scripts/*.sh 2>/dev/null || true
chmod +x "$DEST"/install.sh 2>/dev/null || true

INSTALLED_VERSION="$(cat "$DEST/VERSION" 2>/dev/null || echo unknown)"
echo "Installed alert-manager skill v${INSTALLED_VERSION} -> $DEST"
echo "Re-run this same command any time to update."
