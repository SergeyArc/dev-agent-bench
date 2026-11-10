#!/usr/bin/env bash
# Shared verifier entry point (Harbor falls back to it when a step has no
# tests/test.sh of its own). Runs as root in the task container.
# The step number comes from [steps.verifier.env] BENCH_STEP in task.toml.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR=/logs/verifier

# Hidden tests must not stay readable for the agent of the next step.
chown -R root:root "$TESTS_DIR" 2>/dev/null || true
chmod 0700 "$TESTS_DIR"
cleanup() { find "$TESTS_DIR" -mindepth 1 -delete 2>/dev/null || true; }
trap cleanup EXIT

mkdir -p "$LOG_DIR"
cd "$TESTS_DIR" || exit 1

# No fallback reward on a crash: a missing reward.json makes Harbor report a
# verifier error instead of a silent zero.
PYTHONPATH="$TESTS_DIR" /opt/verifier/bin/python -m benchverify.runner \
  --step "${BENCH_STEP:?BENCH_STEP is not set}" 2>&1 | tee "$LOG_DIR/verifier.log"
exit "${PIPESTATUS[0]}"
