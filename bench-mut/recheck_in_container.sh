#!/usr/bin/env bash
# Check of mutmut's "no tests" verdict, inside the bench image.
#
# mutmut maps tests to functions by calls recorded in the test process while a test runs.
# A function reached only in a subprocess the suite starts (worker, alembic) or only while
# the test modules are imported is reported as "no tests" and never run. Here every such
# mutant gets the whole suite once (`pytest -x`), so the verdict is measured, not assumed.
#
#   recheck_in_container.sh            reads /out/mutants.txt (one mutant name per line),
#                                      writes /out/recheck.tsv: mutant, exit code, seconds
#
# Mounts and environment are those of in_container.sh.
set -euo pipefail

MUTMUT_VERSION="${MUTMUT_VERSION:-3.8.0}"
PYTEST_DB=tracksvc_pytest
DB_URL="postgresql+asyncpg://tracksvc:tracksvc@127.0.0.1:5432/${PYTEST_DB}"
MQ_URL="amqp://tracksvc:tracksvc@127.0.0.1:5672/"
LIMIT="${RECHECK_TIMEOUT:-900}" # the verifier's limit for C4

chmod 0777 /out
as_agent() {
  runuser -u agent -- env -i HOME=/home/agent USER=agent PATH=/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 LANG="${LANG:-C.UTF-8}" UV_EXCLUDE_NEWER="${UV_EXCLUDE_NEWER:-}" \
    DATABASE_URL="$DB_URL" RABBITMQ_URL="$MQ_URL" \
    PYTHONPATH=/app/mutants/src:/bench-mut/plugin UV_PROJECT_ENVIRONMENT=/app/.venv UV_NO_SYNC=1 \
    BENCHMUT_RESET=1 "$@"
}

find /app -mindepth 1 -maxdepth 1 ! -name .venv ! -name .git -exec rm -rf {} +
cp -a /unit/app/. /app/
chown -R agent:agent /app
# The build backend comes from the index: a network failure is retried.
for try in 1 2 3; do
  (cd /app && as_agent uv sync --frozen && as_agent uv pip install --python .venv/bin/python \
    "mutmut==${MUTMUT_VERSION}") >/out/setup.log 2>&1 && break
  [[ "$try" == 3 ]] && { echo "setup failed" >&2; tail -n 5 /out/setup.log >&2; exit 2; }
  sleep 30
done

# The configuration the session used; mutmut generates mutants/ and runs one mutant.
cp /out/setup.cfg /app/setup.cfg
chown agent:agent /app/setup.cfg
as_agent mkdir -p /app/mutants/src
first="$(head -n 1 /out/mutants.txt)"
(cd /app && as_agent .venv/bin/python -m mutmut run --max-children 1 "$first") >/out/generate.log 2>&1

# Restartable: mutants already in recheck.tsv are kept; a STOP file ends the loop.
touch /out/recheck.tsv
rm -f /out/STOP
while IFS= read -r mutant; do
  [[ -n "$mutant" ]] || continue
  [[ -f /out/STOP ]] && { echo "stopped"; exit 0; }
  cut -f1 /out/recheck.tsv | grep -qxF -- "$mutant" && continue
  started="$(date +%s.%N)"
  rc=0
  (cd /app/mutants && as_agent MUTANT_UNDER_TEST="$mutant" timeout -k 10 "$LIMIT" \
    /app/.venv/bin/python -m pytest --rootdir=. -x -q -p no:cacheprovider -p benchmut_reset) \
    >/out/last-run.log 2>&1 || rc=$?
  pkill -KILL -u agent >/dev/null 2>&1 || true
  seconds="$(awk -v a="$started" -v b="$(date +%s.%N)" 'BEGIN { printf "%.1f", b - a }')"
  printf '%s\t%d\t%s\n' "$mutant" "$rc" "$seconds" >>/out/recheck.tsv
  if [[ "$rc" != 0 ]]; then # evidence of what failed
    mkdir -p /out/logs
    tail -n 40 /out/last-run.log >"/out/logs/${mutant}.log"
  fi
done </out/mutants.txt

# Control: no mutant active — the suite must pass in this very setup.
rc=0
(cd /app/mutants && as_agent MUTANT_UNDER_TEST=none \
  /app/.venv/bin/python -m pytest --rootdir=. -x -q -p no:cacheprovider -p benchmut_reset) \
  >/out/control.log 2>&1 || rc=$?
printf 'control\t%d\n' "$rc" >/out/control.tsv
cat /out/control.tsv
