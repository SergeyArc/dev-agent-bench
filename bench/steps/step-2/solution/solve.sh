#!/usr/bin/env bash
# Oracle solution for step-2 (L2-01): makes the project root hold the reference source
# tree (app/, next to this script) — the complete tree after step 2. Files of earlier
# steps that are not part of this tree are removed from src/, tests/ and migrations/;
# everything else in the project root (pyproject.toml, uv.lock, ruff.toml, mypy.ini,
# .venv, .git) is left alone. Idempotent: a second run leaves the same files.
# Works as the unprivileged agent user and as root (ownership then follows the project root).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/app"
DEST="${APP_DIR:-/app}"
PRUNED_DIRS=(src tests migrations)

# The tool configuration is frozen: the solution must never carry or replace it.
for frozen in ruff.toml mypy.ini; do
  if [[ -e "$SRC/$frozen" ]]; then
    echo "solve.sh: $frozen must not be part of the solution" >&2
    exit 1
  fi
done

for dir in "${PRUNED_DIRS[@]}"; do
  [[ -d "$DEST/$dir" ]] || continue
  # Bytecode caches would keep the directory of a removed package alive.
  find "$DEST/$dir" -type d -name __pycache__ -prune -exec rm -rf {} +
  while IFS= read -r -d '' file; do
    [[ -e "$SRC/${file#"$DEST"/}" ]] || rm -f -- "$file"
  done < <(find "$DEST/$dir" -type f -print0)
  find "$DEST/$dir" -depth -type d -empty -delete
done

cp -R "$SRC/." "$DEST/"

if [[ "$(id -u)" == 0 ]]; then
  for entry in "$SRC"/* "$SRC"/.[!.]*; do
    [[ -e "$entry" ]] || continue
    chown -R --reference="$DEST" "$DEST/$(basename "$entry")"
  done
fi

echo "step-2: oracle solution applied to $DEST"
