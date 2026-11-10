#!/usr/bin/env bash
# Builds a plain copy of the project root of one unit under <out>/app. The runs are never
# touched: a snapshot bundle is fetched into a temporary repository and exported from there.
#
#   prepare_unit.sh reference <out>            oracle tree of step 5 on top of the initial /app
#   prepare_unit.sh bundle <bundle> <out>      working copy committed by bench-snapshot
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kind="$1"

case "$kind" in
  reference)
    out="$2"
    mkdir -p "$out/app"
    cp -R "$REPO/bench/environment/app/." "$out/app/"
    APP_DIR="$out/app" bash "$REPO/bench/steps/step-5/solution/solve.sh" >/dev/null
    echo "reference: bench/steps/step-5/solution/app" >"$out/SOURCE"
    ;;
  bundle)
    bundle="$2"
    out="$3"
    mkdir -p "$out/app"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    git -c core.hooksPath=/dev/null init -q --bare "$tmp"
    git -C "$tmp" -c core.hooksPath=/dev/null fetch -q "$bundle" HEAD
    commit="$(git -C "$tmp" rev-parse FETCH_HEAD)"
    git -C "$tmp" archive "$commit" | tar -x -C "$out/app"
    printf 'bundle: %s\ncommit: %s\n' "$bundle" "$commit" >"$out/SOURCE"
    ;;
  *)
    echo "usage: prepare_unit.sh reference <out> | bundle <bundle> <out>" >&2
    exit 2
    ;;
esac
cat "$out/SOURCE"
