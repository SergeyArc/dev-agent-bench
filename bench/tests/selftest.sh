#!/usr/bin/env bash
# Verifier framework self-test. Run in a throwaway container of the base image:
#
#   docker run --rm -v "$PWD/bench/tests:/tests-src:ro" <image> bash /tests-src/selftest.sh
#
# (the image entrypoint starts the services first, then runs this script).
# The self-test rewrites /app and the verifier state: never run it in a trial.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
WORK=/tests-selftest
rm -rf "$WORK"
mkdir -p "$WORK" /logs/verifier
cp -R "$SRC/." "$WORK/"
chown -R root:root "$WORK"
chmod 0700 "$WORK"

cd "$WORK"
PYTHONPATH="$WORK" exec /opt/verifier/bin/python -m benchverify.selftest
