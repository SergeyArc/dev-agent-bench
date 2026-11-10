"""Step 1 (L1-01) deterministic criteria 1.1–1.13 of contract §9.

Every check is independent: it creates the situation it needs (fresh database,
closed port, unset variable), starts the service itself with the commands of
contract §2.2 and stops it again. Nothing here relies on agent-written
scripts, and no check waits without a deadline.

Time limits: the contract limit plus `config.TOLERANCE` (30 + 5 s for the start
command, 10 + 5 s for `/ready`).

Judge criteria of this step are not evaluated here; `JUDGE_CRITERIA` is the
hook for the rubric / judge task. CJ1 does not apply at step 1.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import httpx

from benchverify import config, db, http, proc, reset, toxi
from benchverify.common import Context
from benchverify.result import CheckFailed, Suite

STEP = 1
JUDGE_CRITERIA = ("CJ2", "1.J1", "1.J3")  # hook only: evaluated by the judge stage

# Databases owned by these checks. Their names differ from the default one on
# purpose: the service must use DATABASE_URL, not a hard-coded name (§2.1).
MIGRATION_DB = "tracksvc_step1"
UNMIGRATED_DB = "tracksvc_step1_empty"

MIGRATE_TIMEOUT = 120.0
IMPORT_TIMEOUT = 120.0
HEALTH_LIMIT = config.HEALTH_TIMEOUT + config.TOLERANCE
READY_LIMIT = config.READY_TIMEOUT + config.TOLERANCE
STAY_UP_WINDOW = 3.0  # 1.6: how long the process is watched after the first 200
VERSION_TABLE = "alembic_version"

_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


# --- helpers ------------------------------------------------------------------


def _closed_port_url(kind: str) -> str:
    """URL of a port nothing listens on; the verifier owns this situation."""
    if proc.port_open(config.CLOSED_PORT):
        raise RuntimeError(f"port {config.CLOSED_PORT} is expected to be closed but accepts connections")
    if kind == "database":
        return config.database_url(port=config.CLOSED_PORT)
    return config.rabbitmq_url(port=config.CLOSED_PORT)


def _user_tables(database: str) -> list[str]:
    """Tables of every non-system schema, as `schema.table`."""
    rows = db.query(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' AND table_schema <> ALL(%s) ORDER BY 1, 2",
        [list(_SYSTEM_SCHEMAS)],
        db=database,
    )
    return [f"{schema}.{name}" for schema, name in rows]


def _version_rows(database: str) -> list[tuple[Any, ...]] | None:
    """Rows of `alembic_version`, or None when the table does not exist."""
    found = [t for t in _user_tables(database) if t.split(".", 1)[1] == VERSION_TABLE]
    if len(found) != 1:
        return None
    schema = found[0].split(".", 1)[0]
    return sorted(db.query(f'SELECT * FROM "{schema}"."{VERSION_TABLE}"', db=database))


def _migrate(database: str) -> proc.CommandResult:
    return reset.migrate(config.database_url(database), timeout=MIGRATE_TIMEOUT)


def _expect_migrated(result: proc.CommandResult) -> None:
    if result.timed_out:
        raise CheckFailed(f"`uv run alembic upgrade head` timed out after {MIGRATE_TIMEOUT:.0f} s")
    if result.returncode != 0:
        raise CheckFailed(
            f"`uv run alembic upgrade head` exited with {result.returncode}\n{result.tail(20)}"
        )


@contextmanager
def _app(
    name: str, database_url: str, extra_env: Mapping[str, str] | None = None
) -> Iterator[proc.ManagedProcess]:
    """The HTTP app started with the command of contract §2.2; always stopped."""
    proc.stop_agent_processes()  # port 8000 must be free
    with proc.http_app(
        database_url, log_dir=config.LOG_DIR / "proc", name=f"step1-{name}", extra_env=extra_env
    ) as app:
        yield app


def _wait_listening(app: proc.ManagedProcess) -> None:
    """Poll until the app accepts connections on port 8000.

    Used by criteria about `/ready` or the database: they do not depend on
    what `/health` returns or on how long it takes to answer.
    """
    started = time.monotonic()
    while not proc.port_open(config.HTTP_PORT):
        elapsed = time.monotonic() - started
        if not app.alive():
            raise CheckFailed(
                f"the process exited with code {app.returncode()} after {elapsed:.1f} s "
                f"without listening on port {config.HTTP_PORT}\n{app.log_tail(20)}"
            )
        if elapsed >= HEALTH_LIMIT:
            raise CheckFailed(
                f"the app did not listen on port {config.HTTP_PORT} within {HEALTH_LIMIT:.0f} s\n"
                f"{app.log_tail(20)}"
            )
        time.sleep(0.25)


def _wait_health(app: proc.ManagedProcess, client: httpx.Client) -> tuple[httpx.Response, float]:
    """Poll `GET /health` until 200; fail at the deadline or when the process exits."""
    started = time.monotonic()
    last = "no HTTP response"
    while True:
        try:
            response = client.get("/health", timeout=2.0)
        except httpx.HTTPError as exc:
            last = f"no HTTP response ({exc.__class__.__name__})"
        else:
            if response.status_code == 200:
                return response, round(time.monotonic() - started, 2)
            last = f"status {response.status_code}, body {response.text[:200]!r}"
        elapsed = time.monotonic() - started
        if not app.alive():
            raise CheckFailed(
                f"the process exited with code {app.returncode()} after {elapsed:.1f} s; "
                f"GET /health: {last}\n{app.log_tail(20)}"
            )
        if elapsed >= HEALTH_LIMIT:
            raise CheckFailed(
                f"GET /health did not return 200 within {HEALTH_LIMIT:.0f} s: {last}\n{app.log_tail(20)}"
            )
        time.sleep(0.25)


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _get_ready(client: httpx.Client) -> tuple[httpx.Response, float]:
    """One `GET /ready` bounded by the contract limit plus tolerance."""
    started = time.monotonic()
    try:
        response = client.get("/ready", timeout=READY_LIMIT)
    except httpx.HTTPError as exc:
        raise CheckFailed(
            f"GET /ready gave no response within {READY_LIMIT:.0f} s ({exc.__class__.__name__})"
        ) from exc
    return response, round(time.monotonic() - started, 2)


def _expect_status(response: httpx.Response, wanted: int, what: str) -> None:
    if response.status_code != wanted:
        raise CheckFailed(
            f"{what}: expected {wanted}, got {response.status_code}, body {response.text[:300]!r}"
        )


def _expect_unavailable(response: httpx.Response, what: str) -> None:
    """503 with the error body of contract §5 and code `database_unavailable`."""
    _expect_status(response, 503, what)
    code = http.error_code(response)
    if code != "database_unavailable":
        raise CheckFailed(
            f"{what} -> 503 but error code is {code!r}, expected 'database_unavailable'; "
            f"body {response.text[:300]!r}"
        )
    problem = http.error_body_problem(response)
    if problem is not None:
        raise CheckFailed(
            f"{what} -> 503 database_unavailable but {problem}; body {response.text[:300]!r}"
        )


# --- 1.1–1.3 migration --------------------------------------------------------


def c1_1_migrate_empty_database() -> str:
    db.recreate_database(MIGRATION_DB)
    result = _migrate(MIGRATION_DB)
    _expect_migrated(result)
    return f"exit code 0 on the empty database `{MIGRATION_DB}` ({result.duration_sec} s)"


def c1_2_version_table_one_row() -> str:
    rows = _version_rows(MIGRATION_DB)
    if rows is None:
        raise CheckFailed(
            f"table `{VERSION_TABLE}` not found in database `{MIGRATION_DB}` after the migration; "
            f"tables: {_user_tables(MIGRATION_DB)}"
        )
    if len(rows) != 1:
        raise CheckFailed(f"`{VERSION_TABLE}` has {len(rows)} row(s), expected exactly 1: {rows}")
    return f"`{VERSION_TABLE}` has exactly one row: {rows[0]}"


def c1_3_migrate_again() -> str:
    try:
        before, tables_before = _version_rows(MIGRATION_DB), _user_tables(MIGRATION_DB)
        result = _migrate(MIGRATION_DB)
        _expect_migrated(result)
        after, tables_after = _version_rows(MIGRATION_DB), _user_tables(MIGRATION_DB)
        if before is None or after is None:
            raise CheckFailed(
                f"table `{VERSION_TABLE}` not found in database `{MIGRATION_DB}` "
                f"(before the second run: {before is not None}, after: {after is not None})"
            )
        if after != before:
            raise CheckFailed(f"`{VERSION_TABLE}` changed: {before} -> {after}")
        if tables_after != tables_before:
            raise CheckFailed(f"the set of tables changed: {tables_before} -> {tables_after}")
        return f"second run exited 0; `{VERSION_TABLE}` unchanged: {after}"
    finally:
        db.drop_database(MIGRATION_DB)


# --- 1.4–1.8 HTTP app ---------------------------------------------------------


def c1_4_health() -> str:
    with _app("1.4", config.database_url()) as app, http.client() as client:
        response, took = _wait_health(app, client)
        body = _json(response)
        if body != {"status": "ok"}:
            raise CheckFailed(f'GET /health: body {response.text[:300]!r}, expected {{"status": "ok"}}')
        return f'GET /health -> 200 {{"status": "ok"}} after {took} s'


def c1_5_ready() -> str:
    with _app("1.5", config.database_url()) as app, http.client() as client:
        _wait_listening(app)
        response, took = _get_ready(client)
        _expect_status(response, 200, "GET /ready with PostgreSQL reachable")
        if _json(response) != {"status": "ready"}:
            raise CheckFailed(f'GET /ready: body {response.text[:300]!r}, expected {{"status": "ready"}}')
        return f'GET /ready -> 200 {{"status": "ready"}} in {took} s'


def c1_6_health_without_database() -> str:
    with _app("1.6", _closed_port_url("database")) as app, http.client() as client:
        _, took = _wait_health(app, client)
        deadline = time.monotonic() + STAY_UP_WINDOW
        while time.monotonic() < deadline:
            if not app.alive():
                raise CheckFailed(
                    f"the process exited with code {app.returncode()} after answering /health\n"
                    f"{app.log_tail(20)}"
                )
            time.sleep(0.25)
        try:
            response = client.get("/health", timeout=5.0)
        except httpx.HTTPError as exc:
            raise CheckFailed(f"GET /health stopped answering ({exc.__class__.__name__})") from exc
        _expect_status(response, 200, "GET /health with DATABASE_URL on a closed port")
        return (
            f"DATABASE_URL on closed port {config.CLOSED_PORT}: /health -> 200 after {took} s, "
            f"process alive {STAY_UP_WINDOW:.0f} s later"
        )


def c1_7_ready_without_database() -> str:
    with _app("1.7", _closed_port_url("database")) as app, http.client() as client:
        _wait_listening(app)
        response, took = _get_ready(client)
        _expect_unavailable(response, "GET /ready with DATABASE_URL on a closed port")
        return f"GET /ready -> 503 database_unavailable in {took} s"


def c1_8_no_rabbitmq() -> str:
    variants: list[tuple[str, dict[str, str]]] = [
        ("unset", {}),
        ("closed port", {"RABBITMQ_URL": _closed_port_url("broker")}),
    ]
    notes = []
    for index, (label, extra_env) in enumerate(variants):
        with _app(f"1.8-{index}", config.database_url(), extra_env) as app, http.client() as client:
            health, _ = _wait_health(app, client)
            _expect_status(health, 200, f"GET /health with RABBITMQ_URL {label}")
            ready, _ = _get_ready(client)
            _expect_status(ready, 200, f"GET /ready with RABBITMQ_URL {label}")
        notes.append(f"RABBITMQ_URL {label}: /health 200, /ready 200")
    return "; ".join(notes)


# --- 1.9 packages -------------------------------------------------------------

_IMPORT_MARKER = "BENCH_IMPORTS="
_IMPORT_PROBE = (
    "import importlib, json, sys\n"
    "out = {}\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        module = importlib.import_module(name)\n"
    "    except BaseException as exc:\n"
    "        out[name] = f'{type(exc).__name__}: {exc}'\n"
    "    else:\n"
    "        out[name] = None if hasattr(module, '__path__') else 'is a module, not a package'\n"
    f"print({_IMPORT_MARKER!r} + json.dumps(out))\n"
)


def c1_9_packages_importable() -> str:
    names = [f"{config.PACKAGE}.{layer}" for layer in config.LAYERS]
    result = proc.run_as_agent(
        ["uv", "run", "python", "-c", _IMPORT_PROBE, *names],
        env={"DATABASE_URL": config.database_url()},
        timeout=IMPORT_TIMEOUT,
    )
    if result.timed_out:
        raise CheckFailed(f"importing the layer packages timed out after {IMPORT_TIMEOUT:.0f} s")
    lines = [line for line in result.output.splitlines() if line.startswith(_IMPORT_MARKER)]
    try:
        outcome = dict(json.loads(lines[-1][len(_IMPORT_MARKER) :]))
    except (IndexError, ValueError, TypeError):
        raise CheckFailed(
            f"`uv run python` exited with {result.returncode} before importing anything\n{result.tail(20)}"
        ) from None
    failed = [f"{name}: {outcome.get(name, 'not attempted')}" for name in names if outcome.get(name, "") is not None]
    if failed:
        raise CheckFailed("not importable as a package:\n" + "\n".join(failed))
    return "importable packages: " + ", ".join(names)


# --- 1.10 no DDL from the app --------------------------------------------------


def c1_10_app_creates_no_tables() -> str:
    db.recreate_database(UNMIGRATED_DB)
    try:
        with _app("1.10", config.database_url(UNMIGRATED_DB)) as app, http.client() as client:
            _wait_listening(app)
            _get_ready(client)  # lets a lazily created engine reach the database
            running = _user_tables(UNMIGRATED_DB)
        stopped = _user_tables(UNMIGRATED_DB)
    finally:
        proc.stop_agent_processes()
        db.drop_database(UNMIGRATED_DB)
    if running or stopped:
        raise CheckFailed(
            f"the app left tables in the unmigrated database: {sorted({*running, *stopped})}"
        )
    return "no tables in the unmigrated database after start, GET /ready and shutdown"


# --- 1.11, 1.12 database that accepts connections and does not answer -----------


def _silence(proxy: str) -> None:
    """Drop all data in both directions; connections, old and new, stay open."""
    for stream in ("upstream", "downstream"):
        toxi.add_toxic(proxy, "timeout", {"timeout": 0}, stream=stream)


@contextmanager
def _pg_proxy() -> Iterator[tuple[str, str]]:
    """A toxiproxy listener in front of PostgreSQL: (proxy name, DATABASE_URL through it)."""
    proxy = config.TOXI_PG_PROXY
    listen = f"{config.PG_HOST}:{config.TOXI_PG_PORT}"
    try:
        toxi.ensure_proxy(proxy, listen=listen, upstream=f"{config.PG_HOST}:{config.PG_PORT}")
        if not proc.port_open(config.TOXI_PG_PORT):
            raise RuntimeError(f"the listener {listen} does not accept connections")
        yield proxy, config.database_url(port=config.TOXI_PG_PORT)
    finally:
        proc.stop_agent_processes()
        toxi.delete_proxy(proxy)


def c1_11_ready_with_silent_database() -> str:
    with _pg_proxy() as (proxy, url):
        _silence(proxy)
        with _app("1.11", url) as app, http.client() as client:
            _wait_listening(app)
            response, took = _get_ready(client)
            _expect_unavailable(response, "GET /ready with a database that never answers")
            return f"silent listener: GET /ready -> 503 database_unavailable in {took} s"


def c1_12_ready_after_database_goes_silent() -> str:
    with _pg_proxy() as (proxy, url), _app("1.12", url) as app, http.client() as client:
        _wait_listening(app)
        response, _ = _get_ready(client)
        _expect_status(response, 200, "GET /ready before the database goes silent")
        _silence(proxy)
        response, took = _get_ready(client)
        _expect_unavailable(response, "GET /ready after the database stopped answering")
        return f"GET /ready -> 200, database goes silent, GET /ready -> 503 database_unavailable in {took} s"


# --- 1.13 import without configuration -------------------------------------------

_APP_MODULE = f"{config.PACKAGE}.api.app"
_UNSET_PROBE = (
    "import importlib, json, os, sys\n"
    "for name in ('DATABASE_URL', 'RABBITMQ_URL'):\n"
    "    os.environ.pop(name, None)\n"
    "out = {}\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        module = importlib.import_module(name)\n"
    "    except BaseException as exc:\n"
    "        out[name] = f'{type(exc).__name__}: {exc}'\n"
    "    else:\n"
    "        missing = name.endswith('.app') and not hasattr(module, 'app')\n"
    "        out[name] = 'has no attribute `app`' if missing else None\n"
    f"print({_IMPORT_MARKER!r} + json.dumps(out))\n"
)


def c1_13_import_without_configuration() -> str:
    names = [config.PACKAGE, _APP_MODULE]
    result = proc.run_as_agent(
        ["uv", "run", "python", "-c", _UNSET_PROBE, *names], timeout=IMPORT_TIMEOUT
    )
    if result.timed_out:
        raise CheckFailed(f"importing {_APP_MODULE} timed out after {IMPORT_TIMEOUT:.0f} s")
    lines = [line for line in result.output.splitlines() if line.startswith(_IMPORT_MARKER)]
    try:
        outcome = dict(json.loads(lines[-1][len(_IMPORT_MARKER) :]))
    except (IndexError, ValueError, TypeError):
        raise CheckFailed(
            f"`uv run python` exited with {result.returncode} before importing anything\n{result.tail(20)}"
        ) from None
    failed = [f"{name}: {outcome.get(name, 'not attempted')}" for name in names if outcome.get(name, "") is not None]
    if failed:
        raise CheckFailed(
            "import with DATABASE_URL and RABBITMQ_URL unset failed:\n" + "\n".join(failed)
        )
    return f"with DATABASE_URL and RABBITMQ_URL unset: imported {', '.join(names)}"


def register(suite: Suite, ctx: Context) -> None:
    """Criteria 1.1–1.13, in contract order (1.2 and 1.3 read the database of 1.1)."""
    del ctx  # step 1 needs nothing from the run context
    suite.add("1.1", c1_1_migrate_empty_database, step=STEP)
    suite.add("1.2", c1_2_version_table_one_row, step=STEP)
    suite.add("1.3", c1_3_migrate_again, step=STEP)
    suite.add("1.4", c1_4_health, step=STEP)
    suite.add("1.5", c1_5_ready, step=STEP)
    suite.add("1.6", c1_6_health_without_database, step=STEP)
    suite.add("1.7", c1_7_ready_without_database, step=STEP)
    suite.add("1.8", c1_8_no_rabbitmq, step=STEP)
    suite.add("1.9", c1_9_packages_importable, step=STEP)
    suite.add("1.10", c1_10_app_creates_no_tables, step=STEP)
    suite.add("1.11", c1_11_ready_with_silent_database, step=STEP)
    suite.add("1.12", c1_12_ready_after_database_goes_silent, step=STEP)
    suite.add("1.13", c1_13_import_without_configuration, step=STEP)
