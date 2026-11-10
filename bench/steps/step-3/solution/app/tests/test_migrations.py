import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import RunSql

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def alembic(*arguments: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=PROJECT_ROOT, check=True)


@pytest.fixture
def unmigrated_database(run_sql: RunSql) -> Iterator[None]:
    """Hand the database over without Alembic's bookkeeping and leave it that way."""
    run_sql("DROP TABLE IF EXISTS alembic_version")
    yield
    alembic("downgrade", "base")
    run_sql("DROP TABLE IF EXISTS alembic_version")


@pytest.mark.usefixtures("unmigrated_database")
def test_upgrade_head_records_one_version_and_is_repeatable(run_sql: RunSql) -> None:
    alembic("upgrade", "head")
    first = run_sql("SELECT version_num FROM alembic_version")

    alembic("upgrade", "head")
    second = run_sql("SELECT version_num FROM alembic_version")

    assert len(first) == 1
    assert second == first


@pytest.mark.usefixtures("unmigrated_database")
def test_migrations_use_only_database_url(monkeypatch: pytest.MonkeyPatch, run_sql: RunSql) -> None:
    monkeypatch.delenv("RABBITMQ_URL", raising=False)

    alembic("upgrade", "head")

    assert len(run_sql("SELECT version_num FROM alembic_version")) == 1
