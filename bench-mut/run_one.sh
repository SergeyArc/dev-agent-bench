#!/usr/bin/env bash
# One container for one unit, outside the batch: a check of a single question.
#
#   run_one.sh <unit-dir> <out-dir> clean            the suite on the unmutated tree
#   run_one.sh <unit-dir> <out-dir> mutate <glob>…   precondition, then mutmut on the globs
#
# Refuses to start while the 1-minute load average is 10 or more.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="$(cd "$1" && pwd)"
OUT="$2"
shift 2
IMAGE="${BENCH_IMAGE:-dev-agent-bench:dev}"

load="$(uptime | sed -E 's/.*load averages?: *([0-9.]+).*/\1/')"
if awk -v l="$load" 'BEGIN { exit !(l >= 10) }'; then
  echo "not started: load $load is not below 10" >&2
  exit 2
fi
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
status=0
docker run --rm --name "benchmut-one-$(basename "$UNIT")-$(basename "$OUT")" \
  -v "$UNIT:/unit:ro" -v "$HERE:/bench-mut:ro" -v "$OUT:/out" \
  "$IMAGE" /bench-mut/in_container.sh "$@" >"$OUT/container.log" 2>&1 || status=$?
echo "load at start $load, at end $(uptime | sed -E 's/.*load averages?: *([0-9.]+).*/\1/')" >"$OUT/load.txt"
echo "$(basename "$OUT"): exit $status"
exit "$status"
