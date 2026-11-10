#!/usr/bin/env bash
# Mutation session for one unit: one container per shard, shards in parallel.
#
#   run_unit.sh <unit-dir> <out-dir>
#
# <unit-dir>/app is the project root (see prepare_unit.sh). A shard is one entry of
# src/tracksvc/: a package (layer) or a module; every shard gets its own container, hence
# its own PostgreSQL and RabbitMQ, and runs its mutants one at a time.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="$(cd "$1" && pwd)"
OUT="$2"
IMAGE="${BENCH_IMAGE:-dev-agent-bench:dev}"
PACKAGE_DIR="$UNIT/app/src/tracksvc"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
docker image inspect "$IMAGE" --format '{{.Id}}' >"$OUT/image-id"

shards=()
for entry in "$PACKAGE_DIR"/*; do
  name="$(basename "$entry")"
  if [[ -d "$entry" && "$name" != __pycache__ ]]; then
    shards+=("$name")
  elif [[ "$name" == *.py && "$name" != __init__.py ]]; then
    shards+=("${name%.py}")
  fi
done
printf '%s\n' "${shards[@]}" >"$OUT/shards"

started="$(date +%s)"
pids=()
for shard in "${shards[@]}"; do
  mkdir -p "$OUT/$shard"
  docker run --rm -v "$UNIT:/unit:ro" -v "$HERE:/bench-mut:ro" -v "$OUT/$shard:/out" \
    "$IMAGE" /bench-mut/in_container.sh mutate "tracksvc.${shard}.*" \
    >"$OUT/$shard/container.log" 2>&1 &
  pids+=("$!")
done
status=0
for i in "${!pids[@]}"; do
  wait "${pids[$i]}" || { echo "shard ${shards[$i]}: exit $?" >&2; status=1; }
done
echo "$(( $(date +%s) - started ))" >"$OUT/wall-seconds"
echo "unit $(basename "$UNIT"): ${#shards[@]} shard(s), $(cat "$OUT/wall-seconds") s wall"
exit "$status"
