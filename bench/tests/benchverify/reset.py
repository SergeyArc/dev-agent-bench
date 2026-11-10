"""Verifier reset, exactly per contract §2.2:

    stop processes → drop and recreate the database → delete the contract's
    queues and exchange → migrate → start.

`reset()` performs everything up to and including the migration; starting the
HTTP app / worker is left to the caller (proc.http_app / proc.worker), because
checks start them with different environments.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from . import amqp, config, db, proc, toxi


@dataclass
class ResetResult:
    stopped_processes: int
    database: str
    migrate: proc.CommandResult | None  # None when migrate=False

    @property
    def migrated(self) -> bool:
        return self.migrate is not None and self.migrate.returncode == 0


def ensure_infrastructure() -> None:
    """Make sure PostgreSQL, RabbitMQ and toxiproxy run and the contract
    role / user / proxy exist (the entrypoint is idempotent)."""
    result = subprocess.run(
        [str(config.ENTRYPOINT)], capture_output=True, text=True, timeout=300, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"entrypoint failed: {result.stdout}\n{result.stderr}")
    db.ensure_role()
    amqp.ensure_user()
    toxi.ensure_proxy()


def migrate(database_url: str, *, timeout: float = 300.0) -> proc.CommandResult:
    """`uv run alembic upgrade head` in /app; Alembic reads DATABASE_URL only."""
    return proc.run_as_agent(config.MIGRATE_CMD, env={"DATABASE_URL": database_url}, timeout=timeout)


def downgrade(database_url: str, *, timeout: float = 300.0) -> proc.CommandResult:
    return proc.run_as_agent(config.DOWNGRADE_CMD, env={"DATABASE_URL": database_url}, timeout=timeout)


def reset(database: str = config.DEFAULT_DB, *, migrate_db: bool = True) -> ResetResult:
    stopped = proc.stop_agent_processes()
    db.recreate_database(database)
    amqp.delete_topology()
    toxi.ensure_proxy()
    migration = migrate(config.database_url(database)) if migrate_db else None
    return ResetResult(stopped, database, migration)
