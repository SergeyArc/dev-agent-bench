"""PostgreSQL helpers: administrative reset and SQL row checks."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any

import psycopg

from . import config


def admin_sql(sql: str, *, db: str = "postgres") -> str:
    """Run SQL as the `postgres` superuser (peer auth over the local socket)."""
    result = subprocess.run(
        ["psql", "-v", "ON_ERROR_STOP=1", "-qtAX", "-d", db, "-c", sql],
        user="postgres",
        group="postgres",
        extra_groups=[],
        cwd="/",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"admin SQL failed ({sql!r}): {result.stderr.strip()}")
    return result.stdout.strip()


def ensure_role() -> None:
    """Restore the contract role (the agent may have changed its password)."""
    exists = admin_sql(f"SELECT 1 FROM pg_roles WHERE rolname = '{config.DB_USER}'")
    verb = "ALTER" if exists else "CREATE"
    admin_sql(f"{verb} ROLE {config.DB_USER} LOGIN CREATEDB PASSWORD '{config.DB_PASS}'")


def recreate_database(name: str = config.DEFAULT_DB) -> None:
    """Drop (terminating connections) and create an empty database."""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe database name: {name!r}")
    admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin_sql(f'CREATE DATABASE "{name}" OWNER {config.DB_USER}')


def drop_database(name: str) -> None:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe database name: {name!r}")
    admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def connect(db: str = config.DEFAULT_DB) -> psycopg.Connection[tuple[Any, ...]]:
    return psycopg.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        user=config.DB_USER,
        password=config.DB_PASS,
        dbname=db,
        autocommit=True,
        connect_timeout=10,
    )


def query(sql: str, params: Sequence[Any] | None = None, *, db: str = config.DEFAULT_DB) -> list[tuple[Any, ...]]:
    """Run one statement as the service role and return all rows."""
    with connect(db) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description is not None else []


def execute(sql: str, params: Sequence[Any] | None = None, *, db: str = config.DEFAULT_DB) -> int:
    """Run one statement and return the affected row count."""
    with connect(db) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def tables(db: str = config.DEFAULT_DB, schema: str = "public") -> list[str]:
    rows = query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY 1",
        [schema],
        db=db,
    )
    return [r[0] for r in rows]


def columns(table: str, *, db: str = config.DEFAULT_DB, schema: str = "public") -> dict[str, str]:
    """Column name → data type of a table (empty if the table does not exist)."""
    rows = query(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        [schema, table],
        db=db,
    )
    return {r[0]: r[1] for r in rows}
