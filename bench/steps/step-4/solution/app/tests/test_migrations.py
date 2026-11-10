from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.conftest import RunSql, alembic, assert_error, remove_schema, table_exists
from tracksvc.api.app import create_app

TABLES = "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
COLUMNS = (
    "SELECT table_name, column_name, data_type FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name IN ('tracks', 'track_segments')"
)
# Constraints of a table with their type and columns, e.g. ("tracks", "u", ["code"]).
CONSTRAINTS = """
    SELECT c.conrelid::regclass::text, c.contype::text,
           array(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, position)
                 JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                 ORDER BY k.position),
           c.confrelid::regclass::text
    FROM pg_constraint c
    WHERE c.conrelid IN ('public.tracks'::regclass, 'public.track_segments'::regclass)
"""


@pytest.fixture
def unmigrated_database() -> Iterator[None]:
    """Hand the database over unmigrated; afterwards restore the state it was found in."""
    was_migrated = table_exists("alembic_version")
    remove_schema()
    yield
    remove_schema()
    if was_migrated:
        alembic("upgrade", "head")


@pytest.mark.usefixtures("unmigrated_database")
def test_upgrade_head_records_one_version_and_is_repeatable(run_sql: RunSql) -> None:
    alembic("upgrade", "head")
    first = run_sql("SELECT version_num FROM alembic_version")
    tables = run_sql(TABLES)

    alembic("upgrade", "head")
    second = run_sql("SELECT version_num FROM alembic_version")

    assert len(first) == 1
    assert second == first
    assert run_sql(TABLES) == tables


@pytest.mark.usefixtures("unmigrated_database")
def test_migrations_use_only_database_url(monkeypatch: pytest.MonkeyPatch, run_sql: RunSql) -> None:
    monkeypatch.delenv("RABBITMQ_URL", raising=False)

    alembic("upgrade", "head")

    assert len(run_sql("SELECT version_num FROM alembic_version")) == 1


@pytest.mark.usefixtures("unmigrated_database")
def test_upgrade_head_creates_the_track_tables(run_sql: RunSql) -> None:
    alembic("upgrade", "head")

    columns = {(table, column): data_type for table, column, data_type in run_sql(COLUMNS)}
    assert columns["tracks", "id"] == "uuid"
    assert columns["tracks", "code"] == "character varying"
    assert columns["tracks", "capacity"] == "integer"
    assert columns["tracks", "status"] == "character varying"
    assert columns["track_segments", "track_id"] == "uuid"
    assert columns["track_segments", "label"] == "character varying"
    assert columns["track_segments", "span_start"] == "integer"
    assert columns["track_segments", "span_end"] == "integer"

    constraints = [tuple(row) for row in run_sql(CONSTRAINTS)]
    assert ("tracks", "p", ["id"], "-") in constraints
    assert ("tracks", "u", ["code"], "-") in constraints
    assert ("track_segments", "f", ["track_id"], "tracks") in constraints
    assert ("track_segments", "u", ["track_id", "label"], "-") in constraints


@pytest.mark.usefixtures("unmigrated_database")
def test_downgrade_base_removes_the_schema_and_upgrade_recreates_it(run_sql: RunSql) -> None:
    alembic("upgrade", "head")
    migrated = run_sql(TABLES)
    run_sql(
        "INSERT INTO tracks (id, code, capacity, status) VALUES (gen_random_uuid(), 'A', 1, 'open')"
    )

    alembic("downgrade", "base")
    downgraded = [name for (name,) in run_sql(TABLES)]

    alembic("upgrade", "head")

    assert downgraded == ["alembic_version"]
    assert run_sql("SELECT version_num FROM alembic_version") != []
    assert run_sql(TABLES) == migrated
    assert run_sql("SELECT count(*) FROM tracks")[0][0] == 0


@pytest.mark.usefixtures("unmigrated_database")
def test_the_orm_models_match_the_migrated_schema() -> None:
    alembic("upgrade", "head")

    # Fails if comparing the models with the database would produce a new revision.
    alembic("check")


@pytest.mark.usefixtures("unmigrated_database")
def test_the_application_creates_no_tables_on_an_unmigrated_database(run_sql: RunSql) -> None:
    with TestClient(create_app()) as client:
        response = client.post("/tracks", json={"code": "TRK-A1", "capacity": 100})

        assert_error(response, 503, "database_unavailable")
        assert client.get("/ready").status_code == 200

    assert run_sql(TABLES) == []
