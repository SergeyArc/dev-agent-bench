#!/usr/bin/env bash
# Runs inside the bench image (the entrypoint has already started PostgreSQL and RabbitMQ).
#
#   in_container.sh clean            precondition only: the agent's suite on the unmutated tree
#   in_container.sh mutate <glob>…   precondition, then mutmut on the mutants matching the globs
#
# Mounts: /unit (ro) — <unit>/app, the project root to test; /bench-mut (ro) — this directory;
#         /out — results of this shard.
# The tree replaces /app of the image (its .venv and .git stay), so the suite runs where the
# verifier runs it, as the `agent` user, with the environment of criterion C4.
set -euo pipefail

MODE="${1:?clean|mutate}"
shift || true
MUTMUT_VERSION="${MUTMUT_VERSION:-3.8.0}"
PYTEST_DB=tracksvc_pytest
DB_URL="postgresql+asyncpg://tracksvc:tracksvc@127.0.0.1:5432/${PYTEST_DB}"
MQ_URL="amqp://tracksvc:tracksvc@127.0.0.1:5672/"

mkdir -p /out
chmod 0777 /out
now() { date +%s.%N; }
elapsed() { awk -v a="$1" -v b="$(now)" 'BEGIN { printf "%.1f", b - a }'; }

as_agent() { # as_agent <command…> — C4 environment (benchverify.proc.agent_env)
  runuser -u agent -- env -i HOME=/home/agent USER=agent PATH=/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 LANG="${LANG:-C.UTF-8}" UV_EXCLUDE_NEWER="${UV_EXCLUDE_NEWER:-}" \
    DATABASE_URL="$DB_URL" RABBITMQ_URL="$MQ_URL" "$@"
}

recreate_db() {
  runuser -u postgres -- psql -qtAX -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS \"${PYTEST_DB}\" WITH (FORCE)" \
    -c "CREATE DATABASE \"${PYTEST_DB}\" OWNER tracksvc"
}

clean_run() { # clean_run <label> — `uv run pytest` on /app, like C4
  local label="$1" started rc=0
  recreate_db
  started="$(now)"
  (cd /app && as_agent uv run pytest -p no:cacheprovider) >"/out/${label}.log" 2>&1 || rc=$?
  printf '{"exit_code": %d, "seconds": %s, "summary": "%s"}\n' "$rc" "$(elapsed "$started")" \
    "$(tail -n 1 "/out/${label}.log" | tr -d '"\\')" >"/out/${label}.json"
  pkill -KILL -u agent >/dev/null 2>&1 || true
  cat "/out/${label}.json"
  return "$rc"
}

# --- the unit replaces /app ---------------------------------------------------
find /app -mindepth 1 -maxdepth 1 ! -name .venv ! -name .git -exec rm -rf {} +
cp -a /unit/app/. /app/
chown -R agent:agent /app
# The build backend comes from the index: a network failure is retried, not a verdict.
for try in 1 2 3; do
  (cd /app && as_agent uv sync --frozen) >/out/uv-sync.log 2>&1 && break
  [[ "$try" == 3 ]] && { echo "uv sync failed" >&2; tail -n 5 /out/uv-sync.log >&2; exit 2; }
  sleep 30
done

# --- precondition: the suite passes on the unmutated tree ----------------------
if ! clean_run precondition; then
  echo "precondition failed: the agent's suite does not pass on the unmutated tree" >&2
  exit 3
fi
[[ "$MODE" == clean ]] && exit 0

# --- mutmut ---------------------------------------------------------------------
(cd /app && as_agent uv pip install --python .venv/bin/python "mutmut==${MUTMUT_VERSION}") \
  >/out/mutmut-install.log 2>&1

if grep -qs '^\[tool\.mutmut\]' /app/pyproject.toml || grep -qs '^\[mutmut\]' /app/setup.cfg; then
  echo "the unit carries its own mutmut configuration; refusing to guess" >&2
  exit 4
fi
{
  echo
  echo "[mutmut]"
  echo "source_paths=src/"
  echo "use_git_change_detection=false"
  # A mutant must see what a fresh `uv run pytest` sees. With the default (fork) the worker
  # inherits a process that has already run a pytest session: modules imported there keep
  # objects of that session (alembic binds the captured, later closed stdout), and tests
  # that pass in a fresh process fail — every mutant they cover would count as killed.
  echo "process_isolation=forkserver"
  echo "forkserver_warmup=none"
  # Test hook of batch.py (--test-timeouts): limits so tight that every mutant times out.
  [[ -n "${MUTMUT_TIMEOUT_MULTIPLIER:-}" ]] && echo "timeout_multiplier=${MUTMUT_TIMEOUT_MULTIPLIER}"
  [[ -n "${MUTMUT_TIMEOUT_CONSTANT:-}" ]] && echo "timeout_constant=${MUTMUT_TIMEOUT_CONSTANT}"
  echo "pytest_add_cli_args="
  printf '    %s\n' -p benchmut_reset -p no:cacheprovider
  echo "also_copy="
  # Everything of the project root the suite may read (alembic.ini, migrations, …).
  find /app -mindepth 1 -maxdepth 1 ! -name .venv ! -name .git ! -name src ! -name mutants \
    ! -name tests ! -name setup.cfg ! -name pyproject.toml ! -name uv.lock -printf '    %f\n'
} >>/app/setup.cfg
chown agent:agent /app/setup.cfg
cp /app/setup.cfg /out/setup.cfg

# Additions to the C4 environment, all forced by the tool:
#   PYTHONPATH                  the mutated copy first, also for subprocesses the suite starts
#                               (worker, alembic); then the reset plugin
#   UV_PROJECT_ENVIRONMENT/UV_NO_SYNC  `uv run` started by the suite from mutants/ reuses
#                               /app/.venv instead of building a second environment
# The directory must exist before the interpreter starts: a missing PYTHONPATH entry is
# cached as "nothing to import here" for the whole process.
as_agent mkdir -p /app/mutants/src
# A session stopped by batch.py goes on where it stopped: mutmut keeps the verdicts of the
# meta files as long as the functions are unchanged.
if [[ -f /out/stopped ]]; then
  if [[ -f /out/mutants-meta.tar ]]; then
    tar -xf /out/mutants-meta.tar -C /app/mutants --exclude='mutmut-stats.json' --exclude='mutmut-cicd-stats.json'
    chown -R agent:agent /app/mutants
  fi
  rm -f /out/stopped /out/STOP
fi
recreate_db
started="$(now)"
rc=0
(cd /app && as_agent PYTHONPATH=/app/mutants/src:/bench-mut/plugin \
  UV_PROJECT_ENVIRONMENT=/app/.venv UV_NO_SYNC=1 \
  BENCHMUT_RESET=1 BENCHMUT_RESET_LOG=/out/orphans.log \
  .venv/bin/python -m mutmut run --max-children 1 "$@") >>/out/mutmut-run.log 2>&1 || rc=$?
printf '{"exit_code": %d, "seconds": %s}\n' "$rc" "$(elapsed "$started")" >/out/mutmut-run.json
printf '%d\t%s\n' "$rc" "$(elapsed "$started")" >>/out/mutmut-invocations.tsv
cat /out/mutmut-run.json
tail -c 600 /out/mutmut-run.log | tr '\r' '\n' | tail -n 6

pkill -KILL -u agent >/dev/null 2>&1 || true
# mutmut does not show why its own clean run failed: the suite once more on the instrumented,
# unmutated copy, with the output kept.
if [[ "$rc" != 0 && -d /app/mutants/tests ]]; then
  clean_rc=0
  (cd /app/mutants && as_agent PYTHONPATH=/app/mutants/src:/bench-mut/plugin \
    UV_PROJECT_ENVIRONMENT=/app/.venv UV_NO_SYNC=1 BENCHMUT_RESET=1 MUTANT_UNDER_TEST=none \
    /app/.venv/bin/python -m pytest --rootdir=. -q -p no:cacheprovider -p benchmut_reset) \
    >/out/instrumented-clean.log 2>&1 || clean_rc=$?
  echo "instrumented clean run: exit $clean_rc (see instrumented-clean.log)"
  pkill -KILL -u agent >/dev/null 2>&1 || true
fi
(cd /app/mutants && tar -cf /out/mutants-meta.tar --ignore-failed-read \
  $(find . -name '*.meta' -o -name 'mutmut-stats.json' -o -name 'mutmut-cicd-stats.json')) || true

# --- the environment still serves the unmutated suite ---------------------------
rm -rf /app/mutants /app/setup.cfg
[[ -f /unit/app/setup.cfg ]] && cp -a /unit/app/setup.cfg /app/setup.cfg && chown agent:agent /app/setup.cfg
clean_run postcheck || echo "postcheck failed: the environment was left dirty by the session" >&2
exit "$rc"
