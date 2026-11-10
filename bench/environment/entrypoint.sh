#!/usr/bin/env bash
# Starts PostgreSQL, RabbitMQ and toxiproxy in the background, creates the role
# and database, waits for readiness with bounded retries and exits 0.
# Idempotent: a second call does not start duplicates.
# If arguments are given they are exec'ed after start-up (Harbor keeps the
# container alive with `sh -c "sleep infinity"`).
set -euo pipefail

PG_MAJOR=16
LOG_DIR=/var/log/bench
mkdir -p "$LOG_DIR"

DB_USER=tracksvc
DB_PASS=tracksvc
DB_NAME=tracksvc
MQ_USER=tracksvc
MQ_PASS=tracksvc
TOXI_HOST=127.0.0.1
TOXI_PORT=8474
TOXI_AMQP_LISTEN=127.0.0.1:5673
TOXI_AMQP_UPSTREAM=127.0.0.1:5672

listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

retry() { # retry <attempts> <sleep> <cmd...>
  local n=$1 pause=$2; shift 2
  for ((i = 0; i < n; i++)); do
    "$@" >/dev/null 2>&1 && return 0
    sleep "$pause"
  done
  echo "entrypoint: timed out waiting for: $*" >&2
  return 1
}

psql_admin() { runuser -u postgres -- psql -v ON_ERROR_STOP=1 -qtAX "$@"; }

# --- PostgreSQL ---
if ! runuser -u postgres -- pg_isready -q -h 127.0.0.1 -p 5432; then
  pg_ctlcluster "$PG_MAJOR" main start || true
fi
retry 60 0.5 runuser -u postgres -- pg_isready -q -h 127.0.0.1 -p 5432
if [[ -z "$(psql_admin -c "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'")" ]]; then
  psql_admin -c "CREATE ROLE ${DB_USER} LOGIN CREATEDB PASSWORD '${DB_PASS}'"
fi
if [[ -z "$(psql_admin -c "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'")" ]]; then
  psql_admin -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}"
fi

# --- RabbitMQ ---
if ! rabbitmq-diagnostics -q ping >/dev/null 2>&1; then
  rabbitmq-server -detached >>"$LOG_DIR/rabbitmq-start.log" 2>&1
fi
retry 90 1 rabbitmq-diagnostics -q check_running
retry 30 1 rabbitmq-diagnostics -q check_port_listener 5672
if ! rabbitmqctl -q list_users | awk '{print $1}' | grep -qx "$MQ_USER"; then
  rabbitmqctl -q add_user "$MQ_USER" "$MQ_PASS"
fi
rabbitmqctl -q set_user_tags "$MQ_USER" administrator
rabbitmqctl -q set_permissions -p / "$MQ_USER" '.*' '.*' '.*'

# --- toxiproxy ---
if ! listening "$TOXI_PORT"; then
  setsid nohup toxiproxy-server -host "$TOXI_HOST" -port "$TOXI_PORT" \
    >>"$LOG_DIR/toxiproxy.log" 2>&1 </dev/null &
fi
retry 40 0.25 listening "$TOXI_PORT"
if ! toxiproxy-cli -h "$TOXI_HOST:$TOXI_PORT" inspect rabbitmq >/dev/null 2>&1; then
  toxiproxy-cli -h "$TOXI_HOST:$TOXI_PORT" create \
    -l "$TOXI_AMQP_LISTEN" -u "$TOXI_AMQP_UPSTREAM" rabbitmq >/dev/null
fi

/usr/local/bin/bench-healthcheck

if (($# > 0)); then
  exec "$@"
fi
