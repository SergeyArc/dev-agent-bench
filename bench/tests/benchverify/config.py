"""Fixed facts about the container and the service contract (contract §2, §7.1)."""

from __future__ import annotations

from pathlib import Path

APP_DIR = Path("/app")
SRC_DIR = APP_DIR / "src"
PACKAGE = "tracksvc"
AGENT_USER = "agent"
AGENT_HOME = Path("/home/agent")

VERIFIER_BIN = Path("/opt/verifier/bin")
BASELINE_DIR = Path("/opt/verifier/baseline")
PLUGINS_DIR = Path("/opt/verifier/plugins")
STATE_DIR = Path("/var/lib/bench-verifier")  # root-only, survives across steps
LOG_DIR = Path("/logs/verifier")
ENTRYPOINT = Path("/entrypoint.sh")

PG_HOST = "127.0.0.1"
PG_PORT = 5432
DB_USER = "tracksvc"
DB_PASS = "tracksvc"
DEFAULT_DB = "tracksvc"
PYTEST_DB = "tracksvc_pytest"  # empty, unmigrated; recreated for every C4 run

AMQP_HOST = "127.0.0.1"
AMQP_PORT = 5672
AMQP_USER = "tracksvc"
AMQP_PASS = "tracksvc"
AMQP_VHOST = "/"

TOXI_API = "http://127.0.0.1:8474"
TOXI_AMQP_PROXY = "rabbitmq"
TOXI_AMQP_PORT = 5673
# Created only for criteria 1.11 and 1.12: a listener in front of PostgreSQL that accepts
# connections and never answers.
TOXI_PG_PROXY = "postgres-silent"
TOXI_PG_PORT = 5433
CLOSED_PORT = 59999  # nothing listens here ("unreachable port" of contract §2.1)

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8000
HTTP_BASE = f"http://{HTTP_HOST}:{HTTP_PORT}"

EXCHANGE = "tracksvc.events"
WORK_QUEUE = "tracksvc.track-events"
DEAD_QUEUE = "tracksvc.track-events.dead"
ROUTING_KEY = "track.extend"

MIGRATE_CMD = ["uv", "run", "alembic", "upgrade", "head"]
DOWNGRADE_CMD = ["uv", "run", "alembic", "downgrade", "base"]
HTTP_APP_CMD = [
    "uv", "run", "uvicorn", "tracksvc.api.app:app", "--host", "0.0.0.0", "--port", str(HTTP_PORT),
]
WORKER_CMD = ["uv", "run", "python", "-m", "tracksvc.worker"]
PYTEST_CMD = ["uv", "run", "pytest"]

HEALTH_TIMEOUT = 30.0
READY_TIMEOUT = 10.0  # contract §4.1: `/ready` MUST answer within 10 s
WORKER_READY_TIMEOUT = 30.0
# Contract time limits are requirements on the service; the verifier adds this
# tolerance to start / answer limits (local Docker).
TOLERANCE = 5.0
SIGTERM_LIMIT = 15.0  # contract §7.6: 10 s + 5 s tolerance

LAYERS = ("domain", "application", "infrastructure", "api")
FOUNDATION_ALLOWLIST = frozenset({"pydantic", "typing_extensions"})


def database_url(db: str = DEFAULT_DB, *, host: str = PG_HOST, port: int = PG_PORT) -> str:
    return f"postgresql+asyncpg://{DB_USER}:{DB_PASS}@{host}:{port}/{db}"


def rabbitmq_url(*, port: int = AMQP_PORT, host: str = AMQP_HOST) -> str:
    return f"amqp://{AMQP_USER}:{AMQP_PASS}@{host}:{port}/"
