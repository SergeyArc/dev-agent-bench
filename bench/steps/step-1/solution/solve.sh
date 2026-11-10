#!/usr/bin/env bash
# Oracle solution for step-1 (L1-01): copies the reference source tree (app/, next to
# this script) into the project root. Idempotent: a second run rewrites the same files.
# Works as the unprivileged agent user and as root (ownership then follows the project root).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/app"
DEST="${APP_DIR:-/app}"

# The tool configuration is frozen: the solution must never carry or replace it.
for frozen in ruff.toml mypy.ini; do
  if [[ -e "$SRC/$frozen" ]]; then
    echo "solve.sh: $frozen must not be part of the solution" >&2
    exit 1
  fi
done

cp -R "$SRC/." "$DEST/"

if [[ "$(id -u)" == 0 ]]; then
  for entry in "$SRC"/* "$SRC"/.[!.]*; do
    [[ -e "$entry" ]] || continue
    chown -R --reference="$DEST" "$DEST/$(basename "$entry")"
  done
fi

echo "step-1: oracle solution applied to $DEST"
