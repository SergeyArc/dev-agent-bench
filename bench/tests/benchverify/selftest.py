"""Self-test of the verifier framework: `python -m benchverify.selftest`.

Run inside a throwaway container of the base image (see tests/selftest.sh).
Part A works on the pristine, empty /app: every tool must run and every common
check must report pass/fail/skipped cleanly — never `error`, never a crash.
Part B writes a tiny fixture project into /app to prove that the checks can
both pass and fail for the right reasons, then restores /app.
Part C installs the step-1 fixture service (tests/selftest_fixtures/step1) and
its broken variants: the step-1 criteria pass / fail exactly as expected.
Part D plays a hostile agent: start-up hooks in `.venv`, a
daemonized process on port 8000, git helpers in the repository config. The
snapshot scenarios run `/usr/local/bin/bench-snapshot` of the image: rebuild
the image after changing that script.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pika.exceptions  # type: ignore[import-untyped]

from criteria import step1, step2

from . import amqp, common, config, db, evidence, http, proc, reset, runner, toxi
from .result import CheckFailed, CriterionResult, Status, Suite, run_check
from .state import State

LOGS = config.LOG_DIR / "selftest"


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def status_of(check) -> tuple[Status, str]:  # type: ignore[no-untyped-def]
    return run_check(check)


def expect_status(check, wanted: Status, label: str) -> str:  # type: ignore[no-untyped-def]
    status, evidence = status_of(check)
    expect(status is wanted, f"{label}: expected {wanted.value}, got {status.value}: {evidence}")
    return f"{label}={status.value}"


def agent_write(relative: str, content: str) -> None:
    path = config.APP_DIR / relative
    script = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(sys.argv[2])"
    result = proc.run_as_agent(["python3", "-c", script, str(path), content])
    expect(result.returncode == 0, f"cannot write {path}: {result.output}")


def agent_python(script: str, *args: str) -> proc.CommandResult:
    """Run a Python snippet as the agent user (system interpreter, cwd /app)."""
    result = proc.run_as_agent(["python3", "-c", script, *args])
    expect(result.returncode == 0, f"agent snippet failed: {result.output}")
    return result


def agent_git(*args: str) -> str:
    result = proc.run_as_agent(["git", *args])
    expect(result.returncode == 0, f"git {' '.join(args)} failed: {result.output}")
    return result.output.strip()


def agent_remove(relative: str) -> None:
    result = proc.run_as_agent(["rm", "-rf", str(config.APP_DIR / relative)])
    expect(result.returncode == 0, f"cannot remove {relative}: {result.output}")


def restore_app() -> None:
    for cmd in (["git", "checkout", "-q", "--", "."], ["git", "clean", "-fdxq", "-e", ".venv"]):
        result = proc.run_as_agent(cmd)
        expect(result.returncode == 0, f"{cmd} failed: {result.output}")


# --- part A: infrastructure on the empty /app --------------------------------


def s_infrastructure() -> str:
    reset.ensure_infrastructure()
    before = sorted(p for p in Path("/proc").iterdir() if p.name.isdigit())
    reset.ensure_infrastructure()  # idempotent: no duplicate services
    services = proc.run_as_agent(["ps", "-eo", "args"]).output
    for needle, wanted in (("toxiproxy-server", 1), ("beam.smp", 1), ("postgres -D", 1)):
        count = sum(needle in line for line in services.splitlines())
        expect(count == wanted, f"{needle}: {count} processes, expected {wanted}")
    return f"services up, single instances ({len(before)} processes)"


def s_identity_and_isolation() -> str:
    who = proc.run_as_agent(["id", "-un"]).output.strip()
    expect(who == config.AGENT_USER, f"commands run as {who!r}")
    State().load().save()
    for secret in (config.STATE_DIR, Path(__file__).resolve().parent):
        result = proc.run_as_agent(["ls", str(secret)])
        expect(result.returncode != 0, f"agent user can list {secret}")
    env = proc.run_as_agent(["env"]).output
    expect("DATABASE_URL" not in env and "RABBITMQ_URL" not in env, "service URLs leak into agent env")
    return "agent user cannot read verifier state or tests; env is minimal"


def s_process_control() -> str:
    sleeper = proc.ManagedProcess("sleeper", ["sleep", "300"], {}, LOGS).start()
    expect(sleeper.alive() and sleeper.pid in proc.agent_pids(), "managed process not running as agent")
    stopped = proc.stop_agent_processes()
    expect(stopped >= 1 and not proc.agent_pids(), "agent processes were not stopped")
    sleeper.stop()
    slow = proc.run_as_agent(["sleep", "30"], timeout=1)
    expect(slow.timed_out and not proc.agent_pids(), "timeout did not kill the command")
    stubborn = proc.ManagedProcess(
        "stubborn", ["bash", "-c", "trap '' TERM; sleep 300 & wait"], {}, LOGS
    ).start()
    time.sleep(0.3)
    expect(stubborn.terminate(timeout=1.0) is None, "SIGTERM-ignoring process reported as exited")
    stubborn.kill()
    proc.stop_agent_processes()
    stubborn.stop()
    return f"stopped {stopped} process(es); timeout and SIGKILL paths work"


def s_database() -> str:
    name = f"selftest_{uuid.uuid4().hex[:8]}"
    db.recreate_database(name)
    try:
        db.execute("CREATE TABLE marker (id integer PRIMARY KEY, note text)", db=name)
        expect(db.execute("INSERT INTO marker VALUES (%s, %s)", [1, "x"], db=name) == 1, "insert failed")
        expect(db.query("SELECT id, note FROM marker", db=name) == [(1, "x")], "row mismatch")
        expect(db.tables(name) == ["marker"], "tables() mismatch")
        expect(db.columns("marker", db=name) == {"id": "integer", "note": "text"}, "columns() mismatch")
        holder = db.connect(name)  # an open connection must not block the drop
        db.recreate_database(name)
        holder.close()
        expect(db.tables(name) == [], "database not empty after recreate")
    finally:
        db.drop_database(name)
    return "create / query / forced recreate / drop work"


def s_reset_empty_app() -> str:
    db.execute("CREATE TABLE IF NOT EXISTS leftover (id integer)")
    amqp.declare_topology()
    sleeper = proc.ManagedProcess("leftover", ["sleep", "300"], {}, LOGS).start()
    outcome = reset.reset()
    sleeper.stop()
    expect(outcome.stopped_processes >= 1, "reset did not stop the leftover process")
    expect(db.tables() == [], f"database not recreated: {db.tables()}")
    queues = amqp.all_queues()
    expect(config.WORK_QUEUE not in queues and config.DEAD_QUEUE not in queues, "queues not deleted")
    expect(outcome.migrate is not None and not outcome.migrate.timed_out, "migrate did not run")
    expect(not outcome.migrated, "migration unexpectedly succeeded on the empty /app")
    code = outcome.migrate.returncode if outcome.migrate else None
    return f"reset ok; migrate on empty /app exits {code} (expected failure)"


def s_broker() -> str:
    amqp.delete_topology()
    expect(amqp.consumer_count() == 0 and not amqp.queue_stats().exists, "work queue should not exist")
    expect(amqp.read_dead(timeout=1, settle=0.2) == [], "missing dead queue must read as empty")
    amqp.declare_topology()
    amqp.declare_topology()  # redeclare with identical properties is accepted
    with amqp.Tap() as tap:
        body = json.dumps({"track_id": str(uuid.uuid4()), "delta": 5}).encode()
        amqp.publish(body, message_id="m-1", headers={"x-retry-count": 2, "x-other": "keep"})
        (copy,) = tap.read(expect=1, timeout=5)
        expect(copy.body == body and copy.message_id == "m-1", "tap copy differs")
        expect(copy.headers == {"x-retry-count": 2, "x-other": "keep"}, f"headers: {copy.headers}")
        expect(copy.delivery_mode == 2 and copy.routing_key == config.ROUTING_KEY, "properties differ")
        stats = amqp.queue_stats()
        expect(stats.exists and stats.ready == 1 and stats.unacked == 0 and stats.consumers == 0, f"{stats}")
        amqp.publish(b"not json", exchange="", routing_key=config.DEAD_QUEUE, headers={"x-failure": "invalid_message"})
        (dead,) = amqp.read_dead(expect=1, timeout=5)
        expect(dead.body == b"not json" and dead.message_id is None, "dead message differs")
        expect(dead.headers == {"x-failure": "invalid_message"}, f"dead headers: {dead.headers}")
        expect(len(amqp.drain(config.WORK_QUEUE, expect=1, timeout=5)) == 1, "work queue message lost")
        expect(amqp.wait_idle(timeout=5).ready == 0, "work queue not idle")
        expect(not amqp.wait_consumers(timeout=0.5), "phantom consumer")
    amqp.delete_topology()
    expect(not [q for q in amqp.all_queues() if q.startswith(amqp.TAP_PREFIX)], "tap queue left behind")
    return "publish / tap / dead queue / stats / topology delete work"


def s_toxiproxy() -> str:
    toxi.ensure_proxy()
    conn = amqp.connection(port=config.TOXI_AMQP_PORT)
    channel = conn.channel()
    toxi.disable()
    expect(not toxi.is_enabled(), "proxy still enabled")
    try:
        channel.queue_declare("verifier.tap.toxi-probe", passive=True)
        conn.process_data_events(time_limit=1)
        raise CheckFailed("established connection survived the outage")
    except (pika.exceptions.AMQPError, OSError):
        pass
    try:
        amqp.connection(port=config.TOXI_AMQP_PORT).close()
        raise CheckFailed("new connection succeeded while the proxy was disabled")
    except (pika.exceptions.AMQPError, OSError):
        pass
    expect(amqp.consumer_count() == 0, "direct broker port must stay reachable")
    toxi.enable()
    amqp.connection(port=config.TOXI_AMQP_PORT).close()
    toxi.ensure_proxy()
    return "disable drops and refuses connections; enable restores them"


def s_http_app_on_empty_app() -> str:
    proc.stop_agent_processes()
    with proc.http_app(config.database_url(), log_dir=LOGS, name="app-empty") as app:
        healthy = proc.wait_health(app, timeout=20)
        expect(not healthy, "empty /app answered /health")
        tail = app.log_tail(5)
    expect(not proc.port_open(config.HTTP_PORT), "port 8000 still open")
    return "start command fails cleanly on the empty /app: " + tail.splitlines()[-1][:120]


def s_common_on_empty_app() -> str:
    work = common.make_work_dir()
    ctx = common.Context(step=1, state=State().load(), work_dir=work)
    http.OBSERVED.clear()
    notes = [
        expect_status(common.c1_ruff, Status.PASS, "C1"),
        expect_status(common.c2_mypy, Status.FAIL, "C2"),
        expect_status(lambda: common.c3_layers(ctx), Status.FAIL, "C3"),
        expect_status(lambda: common.c4_pytest(ctx), Status.FAIL, "C4"),
        expect_status(common.c6_no_http_500, Status.SKIPPED, "C6"),
        expect_status(common.c7_configs_unchanged, Status.PASS, "C7"),
        expect_status(common.c8_no_dynamic_imports, Status.SKIPPED, "C8"),
    ]
    return ", ".join(notes)


# --- part B: fixture project --------------------------------------------------

FIXTURE = {
    "src/tracksvc/__init__.py": "",
    "src/tracksvc/domain/__init__.py": "",
    "src/tracksvc/domain/model.py": (
        "from __future__ import annotations\n\nfrom dataclasses import dataclass\n\n"
        "from pydantic import BaseModel\n\n\n@dataclass(frozen=True)\nclass Span:\n"
        "    \"\"\"Half-open interval [start, end).\"\"\"\n\n"
        "    start: int\n    end: int\n\n\nclass Payload(BaseModel):\n"
        "    \"\"\"Wire form of a span start.\"\"\"\n\n    span_start: int\n"
    ),
    "src/tracksvc/application/__init__.py": "",
    "src/tracksvc/application/use_cases.py": (
        "from tracksvc.domain.model import Span\n\n\ndef width(span: Span) -> int:\n"
        "    \"\"\"Return the number of units the span covers.\"\"\"\n"
        "    return span.end - span.start\n"
    ),
    "src/tracksvc/infrastructure/__init__.py": "",
    "src/tracksvc/infrastructure/store.py": (
        "from tracksvc.application.use_cases import width\nfrom tracksvc.domain.model import Span\n\n\n"
        "def stored_width() -> int:\n    \"\"\"Return the width of the stored span.\"\"\"\n"
        "    return width(Span(0, 1))\n"
    ),
    "src/tracksvc/api/__init__.py": "",
    "src/tracksvc/api/app.py": (
        "from fastapi import FastAPI\n\nfrom tracksvc.infrastructure.store import stored_width\n\n"
        "app = FastAPI()\n\n\n@app.get(\"/health\")\nasync def health() -> dict[str, str]:\n"
        "    \"\"\"Report that the process is up.\"\"\"\n"
        "    return {\"status\": \"ok\"}\n\n\n@app.get(\"/boom\")\nasync def boom() -> dict[str, int]:\n"
        "    \"\"\"Fail on purpose to produce a 500.\"\"\"\n"
        "    return {\"value\": stored_width() // 0}\n"
    ),
    "tests/test_width.py": (
        "from tracksvc.application.use_cases import width\nfrom tracksvc.domain.model import Span\n\n\n"
        "def test_width() -> None:\n    assert width(Span(1, 4)) == 3\n\n\n"
        "def test_other() -> None:\n    assert width(Span(0, 1)) == 1\n"
    ),
}


def s_fixture_passes() -> str:
    restore_app()
    for relative, content in FIXTURE.items():
        agent_write(relative, content)
    state = State().load()
    state.data["steps"].pop("1", None)
    work = common.make_work_dir()
    ctx = common.Context(step=1, state=state, work_dir=work)
    notes = [
        expect_status(common.c1_ruff, Status.PASS, "C1"),
        expect_status(common.c2_mypy, Status.PASS, "C2"),
        expect_status(lambda: common.c3_layers(ctx), Status.PASS, "C3"),
        expect_status(lambda: common.c4_pytest(ctx), Status.PASS, "C4"),
        expect_status(common.c7_configs_unchanged, Status.PASS, "C7"),
        expect_status(common.c8_no_dynamic_imports, Status.PASS, "C8"),
    ]
    expect(
        ctx.pytest_passed == ["tests/test_width.py::test_other", "tests/test_width.py::test_width"],
        f"node IDs: {ctx.pytest_passed}",
    )
    expect(ctx.pytest_collected == ctx.pytest_passed, f"collected: {ctx.pytest_collected}")
    return ", ".join(notes)


def s_fixture_http() -> str:
    http.OBSERVED.clear()
    with proc.http_app(None, log_dir=LOGS, name="app-fixture") as app:
        expect(proc.wait_health(app), "fixture app did not become healthy:\n" + app.log_tail())
        pid = app.service_pid()
        expect(pid is not None and pid in proc.agent_pids(), "service PID not found")
        with http.client() as client:
            expect(client.get("/health").json() == {"status": "ok"}, "health body")
            expect_status(common.c6_no_http_500, Status.PASS, "C6")
            expect(client.get("/boom").status_code == 500, "expected a 500 from /boom")
        expect_status(common.c6_no_http_500, Status.FAIL, "C6")
        app.kill()
        expect(not app.alive(), "SIGKILL did not stop the app")
        expect(proc.wait_until(lambda: not proc.port_open(config.HTTP_PORT), 5.0), "port 8000 still open")
    with proc.http_app(None, log_dir=LOGS, name="app-fixture-term") as app:
        expect(proc.wait_health(app), "fixture app did not restart")
        took = app.terminate()
        expect(took is not None, "app did not exit on SIGTERM")
    http.OBSERVED.clear()
    return f"start / health / C6 / SIGKILL / SIGTERM ({took}s) work"


SUITE_BEFORE = (
    "import pytest\n\nfrom tracksvc.application.use_cases import width\n"
    "from tracksvc.domain.model import Span\n\n\n"
    "def test_strict() -> None:\n    span = Span(2, 5)\n    assert width(span) == 3\n"
    "    assert span.start == 2\n    with pytest.raises(AttributeError):\n"
    "        span.missing  # type: ignore[attr-defined]\n\n\n"
    "def test_plain() -> None:\n    assert width(Span(0, 2)) == 2\n\n\n"
    "def test_later() -> None:\n    assert width(Span(0, 3)) == 3\n\n\n"
    "class TestGroup:\n    def test_member(self) -> None:\n        assert width(Span(1, 2)) == 1\n\n\n"
    "def test_doomed() -> None:\n    assert width(Span(0, 4)) == 4\n"
)
# test_doomed deleted, test_strict lost an assert and its pytest.raises,
# test_plain skipped, test_later xfail'ed, TestGroup skipped as a class.
SUITE_AFTER = (
    "import pytest\n\nfrom tracksvc.application.use_cases import width\n"
    "from tracksvc.domain.model import Span\n\n\n"
    "def test_strict() -> None:\n    span = Span(2, 5)\n    assert width(span) == 3\n\n\n"
    "@pytest.mark.skip(reason=\"later\")\n"
    "def test_plain() -> None:\n    assert width(Span(0, 2)) == 2\n\n\n"
    "def test_later() -> None:\n    pytest.xfail(\"later\")\n    assert width(Span(0, 3)) == 3\n\n\n"
    "@pytest.mark.skipif(True, reason=\"later\")\n"
    "class TestGroup:\n    def test_member(self) -> None:\n        assert width(Span(1, 2)) == 1\n"
)


# Only moved or renamed: test_doomed -> another file and name with the same body,
# test_later -> another file under the same name, without its assertion.
_LATER = "def test_later() -> None:\n    assert width(Span(0, 3)) == 3\n\n\n"
_DOOMED = "\n\ndef test_doomed() -> None:\n    assert width(Span(0, 4)) == 4\n"
SUITE_MOVED_REST = SUITE_BEFORE.replace(_LATER, "").replace(_DOOMED, "")
SUITE_MOVED_NEW = (
    "from tracksvc.application.use_cases import width\nfrom tracksvc.domain.model import Span\n\n\n"
    "def test_renamed() -> None:\n    assert width(Span(0, 4)) == 4\n\n\n"
    "def test_later() -> None:\n    width(Span(0, 3))\n"
)

# C4 environment: its own reachable, empty, unmigrated database and the broker.
ENV_TEST = (
    "import asyncio\nimport os\nimport socket\nfrom urllib.parse import urlsplit\n\nimport asyncpg\n\n\n"
    "async def _public_tables(url: str) -> int:\n"
    "    conn = await asyncpg.connect(url.replace(\"+asyncpg\", \"\"))\n    try:\n"
    "        count = await conn.fetchval(\n"
    "            \"SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'\"\n"
    "        )\n        await conn.execute(\"CREATE TABLE leftover (id integer)\")\n"
    "        return int(count)\n    finally:\n        await conn.close()\n\n\n"
    "def test_env() -> None:\n    url = os.environ[\"DATABASE_URL\"]\n"
    "    assert url.endswith(\"/" + config.PYTEST_DB + "\")\n"
    "    assert asyncio.run(_public_tables(url)) == 0\n"
    "    broker = urlsplit(os.environ[\"RABBITMQ_URL\"])\n"
    "    socket.create_connection((broker.hostname, broker.port), timeout=5).close()\n"
)


def s_c4_semantics() -> str:
    """C4 = `uv run pytest` exits 0 and >= 1 test ran; nothing else."""
    state = State().load()
    ctx = common.Context(step=2, state=state, work_dir=common.make_work_dir())
    notes = []

    # A test recorded at the previous step is gone: not a C4 matter any more.
    state.data["steps"]["1"] = {
        "criteria": {},
        "pytest_passed": ["tests/test_width.py::test_width", "tests/test_gone.py::test_gone"],
        "pytest_collected": ["tests/test_width.py::test_width", "tests/test_gone.py::test_gone"],
    }
    status, text = status_of(lambda: common.c4_pytest(ctx))
    expect(status is Status.PASS and "2 test(s) passed" in text, f"C4 missing ID: {status} {text}")
    notes.append("missing node ID -> pass")

    # Environment: an empty, unmigrated database of its own, recreated for every
    # run and removed afterwards; the service database is left alone.
    db.recreate_database()
    db.execute("CREATE TABLE service_marker (id integer)")
    agent_write("tests/test_env.py", ENV_TEST)
    try:
        for attempt in (1, 2):  # the table left by run 1 must be gone in run 2
            status, text = status_of(lambda: common.c4_pytest(ctx))
            expect(status is Status.PASS and "3 test(s) passed" in text, f"C4 env, run {attempt}: {status} {text}")
    finally:
        agent_remove("tests/test_env.py")
    expect(db.tables() == ["service_marker"], f"service database touched: {db.tables()}")
    left = db.admin_sql(f"SELECT count(*) FROM pg_database WHERE datname = '{config.PYTEST_DB}'")
    expect(left == "0", "pytest database not dropped after C4")
    db.recreate_database()
    notes.append("own empty database + broker reachable")

    original = FIXTURE["tests/test_width.py"]
    try:
        # A failing test: pytest exits 1. The collected IDs are still recorded.
        agent_write("tests/test_width.py", original + "\n\ndef test_red() -> None:\n    assert False\n")
        status, text = status_of(lambda: common.c4_pytest(ctx))
        expect(status is Status.FAIL and "exited with 1" in text, f"C4 red: {status} {text}")
        expect(ctx.pytest_passed is None, "passed IDs recorded for a red run")
        expect(
            ctx.pytest_collected is not None and len(ctx.pytest_collected) == 3,
            f"collected IDs of a red run: {ctx.pytest_collected}",
        )
        notes.append("failing test -> fail")

        # Exit code 0 without a single executed test.
        agent_write(
            "tests/test_width.py",
            "import pytest\n\n\n@pytest.mark.skip(reason=\"x\")\ndef test_width() -> None:\n    assert True\n",
        )
        status, text = status_of(lambda: common.c4_pytest(ctx))
        expect(status is Status.FAIL and "no test passed" in text, f"C4 all skipped: {status} {text}")
        notes.append("exit 0 with every test skipped -> fail")
    finally:
        agent_write("tests/test_width.py", original)
    return "; ".join(notes)


GUARD_TEST = (
    "import os\n\n\ndef test_envguard() -> None:\n"
    "    assert \"PYTEST_ADDOPTS\" not in os.environ\n"
    "    assert \"PYTEST_PLUGINS\" not in os.environ\n"
)
PYTEST_HIJACK = {"PYTEST_ADDOPTS": "-k test_other", "PYTEST_PLUGINS": "bench_no_such_plugin"}


def s_c4_pytest_env() -> str:
    """C4 never passes PYTEST_ADDOPTS / PYTEST_PLUGINS on; the agent's own
    pytest configuration files stay in force."""
    ctx = common.Context(step=2, state=State().load(), work_dir=common.make_work_dir())
    pyproject = (config.APP_DIR / "pyproject.toml").read_text()
    saved = {name: os.environ.get(name) for name in PYTEST_HIJACK}
    agent_write("tests/test_envguard.py", GUARD_TEST)
    try:
        # From the verifier's own environment and from the per-step extras.
        os.environ.update(PYTEST_HIJACK)
        ctx.pytest_env = dict(PYTEST_HIJACK)
        status, text = status_of(lambda: common.c4_pytest(ctx))
        expect(status is Status.PASS and "3 test(s) passed" in text, f"C4 with hijacked env: {status} {text}")
        ctx.pytest_env = {}

        agent_write("pytest.ini", "[pytest]\naddopts = -k test_other\n")
        status, text = status_of(lambda: common.c4_pytest(ctx))
        expect(status is Status.PASS and "1 test(s) passed" in text, f"C4 with pytest.ini: {status} {text}")
        agent_remove("pytest.ini")

        agent_write("pyproject.toml", pyproject + "\n[tool.pytest.ini_options]\naddopts = \"-k envguard\"\n")
        status, text = status_of(lambda: common.c4_pytest(ctx))
        expect(status is Status.PASS and "1 test(s) passed" in text, f"C4 with pyproject.toml: {status} {text}")
        expect(ctx.pytest_passed == ["tests/test_envguard.py::test_envguard"], f"node IDs: {ctx.pytest_passed}")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        agent_remove("pytest.ini")
        agent_remove("tests/test_envguard.py")
        agent_write("pyproject.toml", pyproject)
    return "PYTEST_ADDOPTS / PYTEST_PLUGINS dropped; addopts of pytest.ini and pyproject.toml honoured"


def collect_cj1(
    step: int, state: State, node_ids: list[str] | None, logs: Path
) -> tuple[CriterionResult, dict[str, Any]]:
    result, data = evidence.collect(step, state, node_ids, logs)
    if result is None or result.status is not Status.EVIDENCE or data is None:
        raise CheckFailed(f"CJ1 evidence not collected: {result}")
    return result, data


def cj1_evidence(
    step: int, state: State, ctx: common.Context, logs: Path
) -> tuple[CriterionResult, dict[str, Any]]:
    return collect_cj1(step, state, ctx.pytest_collected, logs)


def s_cj1_evidence() -> str:
    """Evidence collector: removed test, added skip / xfail, removed assertions."""
    state = State().load()
    state.data = {"steps": {}}
    logs = LOGS / "cj1"
    ctx = common.Context(step=1, state=state, work_dir=common.make_work_dir())
    try:
        # Step 1: baseline suite. No CJ1 at step 1, only the snapshot.
        agent_write("tests/test_suite.py", SUITE_BEFORE)
        expect_status(lambda: common.c4_pytest(ctx), Status.PASS, "C4")
        expect(len(ctx.pytest_collected or []) == 7, f"step 1 collected: {ctx.pytest_collected}")
        # A symlink to a root-only file must not be copied by the (root) collector.
        state.save()
        leak = config.APP_DIR / "tests/leak.txt"
        linked = proc.run_as_agent(["ln", "-s", str(state.path), str(leak)])
        expect(linked.returncode == 0, f"cannot create the symlink: {linked.output}")
        first = evidence.collect(1, state, ctx.pytest_collected, logs)
        expect(first == (None, None), f"CJ1 reported at step 1: {first}")
        stored = evidence.load_snapshot(1)
        expect(
            stored is not None and set(stored.files) == {"tests/test_suite.py", "tests/test_width.py"},
            f"snapshot files (symlink must be left out): {stored and sorted(stored.files)}",
        )
        leak_flag = {"path": "tests/leak.txt", "reason": "symlink", "detail": f"-> {state.path}"}
        expect(
            stored is not None and stored.skipped == [leak_flag],
            f"the symlink must be flagged: {stored and stored.skipped}",
        )
        state.record_step(1, [], ctx.pytest_passed, ctx.pytest_collected)
        agent_remove("tests/leak.txt")

        # Step 2, nothing changed: evidence is empty.
        ctx.step = 2
        expect_status(lambda: common.c4_pytest(ctx), Status.PASS, "C4")
        result, data = cj1_evidence(2, state, ctx, logs)
        expect(not result.scored, "CJ1 must carry no weight")
        # The symlink left out at step 1 makes the comparison incomplete.
        expect(
            data["evidence_incomplete"] is True
            and data["incomplete"] == {"previous": [leak_flag], "current": [], "diff": []},
            f"incompleteness of the previous snapshot: {data['incomplete']}",
        )
        expect(
            data["missing_node_ids"] == [] and not data["skip_markers_added"]
            and not data["assertions_removed"] and data["diff"] == "",
            f"evidence on an unchanged suite: {data}",
        )

        # Step 2, weakened suite: still green for C4, all three kinds of evidence.
        agent_write("tests/test_suite.py", SUITE_AFTER)
        expect_status(lambda: common.c4_pytest(ctx), Status.PASS, "C4")
        result, data = cj1_evidence(2, state, ctx, logs)
        prefix = "tests/test_suite.py::"
        expect(data["missing_node_ids"] == [prefix + "test_doomed"], f"missing: {data['missing_node_ids']}")
        expect(data["moved_or_renamed"] == [], f"moved: {data['moved_or_renamed']}")
        markers = {(m["where"], m["marker"]) for m in data["skip_markers_added"]}
        wanted = {
            (prefix + "test_plain", "skip"),
            (prefix + "test_later", "xfail"),
            (prefix + "TestGroup", "skipif"),
        }
        expect(markers == wanted, f"markers: {data['skip_markers_added']}")
        expect(
            data["assertions_removed"]
            == [
                {
                    "test": prefix + "test_strict",
                    "before": {"asserts": 2, "raises": 1},
                    "after": {"asserts": 1, "raises": 0},
                }
            ],
            f"assertions: {data['assertions_removed']}",
        )
        expect(data["files"] == {"added": [], "removed": [], "changed": ["tests/test_suite.py"]}, f"{data['files']}")
        for line in ("--- a/tests/test_suite.py", "-    assert span.start == 2", "+@pytest.mark.skip("):
            expect(line in data["diff"], f"diff lacks {line!r}:\n{data['diff']}")
        expect("test_doomed" in result.evidence and "3 -> 1" in result.evidence, f"summary: {result.evidence}")
        written = json.loads((logs / evidence.OUTPUT_NAME).read_text())
        kept = json.loads((evidence.ROOT / "step-2" / "cj1.json").read_text())
        expect(written == data == kept, "evidence files differ from the returned data")

        # Moved / renamed tests are paired (by body, then by name), not missing.
        agent_write("tests/test_suite.py", SUITE_MOVED_REST)
        agent_write("tests/unit/test_moved.py", SUITE_MOVED_NEW)
        expect_status(lambda: common.c4_pytest(ctx), Status.PASS, "C4")
        result, data = cj1_evidence(2, state, ctx, logs)
        target = "tests/unit/test_moved.py::"
        expect(data["missing_node_ids"] == [], f"moved tests reported missing: {data['missing_node_ids']}")
        expect(
            data["moved_or_renamed"]
            == [
                {
                    "from": prefix + "test_doomed",
                    "to": target + "test_renamed",
                    "matched_by": "body",
                    "node_ids": [prefix + "test_doomed"],
                },
                {
                    "from": prefix + "test_later",
                    "to": target + "test_later",
                    "matched_by": "name",
                    "node_ids": [prefix + "test_later"],
                },
            ],
            f"moved: {data['moved_or_renamed']}",
        )
        expect(
            data["assertions_removed"]
            == [
                {
                    "test": target + "test_later",
                    "moved_from": prefix + "test_later",
                    "before": {"asserts": 1, "raises": 0},
                    "after": {"asserts": 0, "raises": 0},
                }
            ],
            f"assertions of a moved test: {data['assertions_removed']}",
        )
        expect(not data["skip_markers_added"], f"markers: {data['skip_markers_added']}")
        expect("moved/renamed (body)" in result.evidence, f"summary: {result.evidence}")
        agent_remove("tests/unit")

        # A removed test file and an unparsable one.
        agent_remove("tests/test_suite.py")
        agent_write("tests/test_width.py", FIXTURE["tests/test_width.py"] + "def broken(:\n")
        expect_status(lambda: common.c4_pytest(ctx), Status.FAIL, "C4")
        result, data = cj1_evidence(2, state, ctx, logs)
        expect(data["files"]["removed"] == ["tests/test_suite.py"], f"removed files: {data['files']}")
        expect(data["unparsable"]["current"] == ["tests/test_width.py: SyntaxError"], f"{data['unparsable']}")

        # Step 3 without a snapshot of step 2: skipped, not an error.
        evidence.save_snapshot(2, evidence.Snapshot())
        (evidence.ROOT / "step-2" / "tests.json").unlink()
        skipped, nothing = evidence.collect(3, state, None, logs)
        expect(
            skipped is not None and skipped.status is Status.SKIPPED and nothing is None,
            f"CJ1: {skipped}",
        )
    finally:
        agent_remove("tests/leak.txt")
        agent_remove("tests/test_suite.py")
        agent_remove("tests/unit")
        agent_write("tests/test_width.py", FIXTURE["tests/test_width.py"])
    return (
        "removed test, moved/renamed tests, added skip/xfail/skipif, removed assert + pytest.raises, "
        "tests/ diff, symlink ignored"
    )


WRITE_LINES = "import sys; open(sys.argv[1], 'w').write(sys.argv[2] * int(sys.argv[3]))"


def s_cj1_incomplete() -> str:
    """CJ1 evidence says what it could not see: symlinks, oversize and non-UTF-8
    files, a cut diff. Flags only: the status stays `evidence`, unscored."""
    state = State().load()
    state.data = {"steps": {}}
    logs = LOGS / "cj1-incomplete"
    tests = config.APP_DIR / "tests"
    extras = ["link.py", "linked_dir", "huge.py", "latin1.py", "pipe.py", "zz_a.py", "zz_b.py", "zz_c.py"]
    try:
        expect(evidence.collect(1, state, None, logs) == (None, None), "CJ1 reported at step 1")
        state.record_step(1, [], None, None)
        result, data = collect_cj1(2, state, None, logs)
        expect(
            data["evidence_incomplete"] is False
            and data["incomplete"] == {"previous": [], "current": [], "diff": []}
            and data["diff_truncated"] is False,
            f"flags on a complete snapshot: {data['incomplete']}",
        )
        expect(result.evidence.startswith("evidence complete"), f"summary: {result.evidence}")

        proc.run_as_agent(["ln", "-s", "/etc/passwd", str(tests / "link.py")])
        proc.run_as_agent(["ln", "-s", "/etc", str(tests / "linked_dir")])
        proc.run_as_agent(["mkfifo", "-m", "0644", str(tests / "pipe.py")])
        agent_python(WRITE_LINES, str(tests / "huge.py"), "# pad\n", str(evidence.MAX_FILE_BYTES // 6 + 1))
        agent_python("import sys; open(sys.argv[1], 'wb').write(b'x = 1\\n# caf\\xe9\\n')", str(tests / "latin1.py"))
        line = "# filler\n"  # 9 characters; 10 in the diff
        count = evidence.DIFF_LIMIT * 3 // 4 // 10
        for name in ("zz_a.py", "zz_b.py", "zz_c.py"):
            agent_python(WRITE_LINES, str(tests / name), line, str(count))

        result, data = collect_cj1(2, state, None, logs)
        expect(not result.scored, f"flags must not be a verdict: {result}")
        expect(data["evidence_incomplete"] is True, "evidence_incomplete not raised")
        current = {(item["path"], item["reason"]): item["detail"] for item in data["incomplete"]["current"]}
        wanted = {
            ("tests/link.py", "symlink"): "-> /etc/passwd",
            ("tests/linked_dir", "symlink"): "directory -> /etc",
            ("tests/pipe.py", "not_regular_file"): "mode prw-r--r--",
            ("tests/huge.py", "too_large"): f"{(evidence.MAX_FILE_BYTES // 6 + 1) * 6} bytes, limit {evidence.MAX_FILE_BYTES}",
            ("tests/latin1.py", "not_utf8"): "invalid byte at offset 11",
        }
        expect(current == wanted, f"flags of the current snapshot: {data['incomplete']['current']}")
        expect(data["incomplete"]["previous"] == [], f"previous: {data['incomplete']['previous']}")
        lost = [(item["path"], item["reason"]) for item in data["incomplete"]["diff"]]
        expect(
            lost == [("tests/zz_b.py", "diff_cut"), ("tests/zz_c.py", "diff_omitted")],
            f"diff flags: {data['incomplete']['diff']}",
        )
        expect(data["diff_truncated"] is True and len(data["diff"]) == evidence.DIFF_LIMIT, "diff not cut to the limit")
        expect("+++ b/tests/zz_b.py" in data["diff"] and "zz_c.py" not in data["diff"], "diff content")
        expect("passwd" not in json.dumps(data["files"]) and "root:" not in data["diff"], "symlink target was read")
        expect(result.evidence.startswith("EVIDENCE INCOMPLETE: 7 item(s)"), f"summary: {result.evidence}")
        for needle in ("incomplete (current): symlink tests/link.py -> /etc/passwd", "incomplete (diff): diff_cut tests/zz_b.py"):
            expect(needle in result.evidence, f"summary lacks {needle!r}: {result.evidence}")
        written = json.loads((logs / evidence.OUTPUT_NAME).read_text())
        expect(written == data, "evidence file differs from the returned data")

        # The gaps of this step are reported as `previous` at the next one.
        state.record_step(2, [], None, None)
        result, data = collect_cj1(3, state, None, logs)
        previous = {(item["path"], item["reason"]) for item in data["incomplete"]["previous"]}
        expect(previous == set(wanted) and data["evidence_incomplete"] is True, f"previous: {data['incomplete']}")
        expect(data["diff"] == "" and data["incomplete"]["diff"] == [], "diff of an unchanged suite")

        # tests/ itself replaced by a symlink.
        agent_python("import os, sys; os.rename(sys.argv[1], sys.argv[1] + '.real'); os.symlink('/etc', sys.argv[1])", str(tests))
        try:
            result, data = collect_cj1(3, state, None, logs)
        finally:
            agent_python("import os, sys; os.unlink(sys.argv[1]); os.rename(sys.argv[1] + '.real', sys.argv[1])", str(tests))
        expect(
            data["incomplete"]["current"]
            == [{"path": "tests", "reason": "symlink", "detail": "tests/ itself is a symlink -> /etc"}],
            f"tests/ symlink: {data['incomplete']['current']}",
        )
    finally:
        for name in extras:
            agent_remove(f"tests/{name}")
    return "symlink (file, directory, tests/), oversize, non-UTF-8, FIFO, cut and omitted diff are flagged; no verdict"


def s_fixture_violations() -> str:
    state = State().load()
    work = common.make_work_dir()
    ctx = common.Context(step=2, state=state, work_dir=work)
    notes = []

    # C3 (AST): application imports a framework; C3 (import-linter): domain imports infrastructure.
    agent_write("src/tracksvc/application/bad.py", "import sqlalchemy\n\nX = sqlalchemy\n")
    status, evidence = status_of(lambda: common.c3_layers(ctx))
    expect(status is Status.FAIL and "application imports sqlalchemy" in evidence, f"C3 AST: {evidence}")
    agent_write("src/tracksvc/application/bad.py", "")
    agent_write("src/tracksvc/domain/bad.py", "from tracksvc.infrastructure import store\n\nX = store\n")
    status, evidence = status_of(lambda: common.c3_layers(ctx))
    expect(status is Status.FAIL and "import-linter" in evidence, f"C3 import-linter: {evidence}")
    expect("domain imports tracksvc.infrastructure" in evidence, f"C3 AST (internal): {evidence}")
    notes.append("C3 layer violations")

    # C8: dynamic import in domain.
    agent_write("src/tracksvc/domain/bad.py", "import importlib\n\nX = importlib.import_module(\"json\")\n")
    expect_status(common.c8_no_dynamic_imports, Status.FAIL, "C8")
    agent_write("src/tracksvc/domain/bad.py", "X = __import__(\"json\")\n")
    expect_status(common.c8_no_dynamic_imports, Status.FAIL, "C8")
    agent_write("src/tracksvc/domain/bad.py", "")
    notes.append("C8 dynamic imports")

    # C1 / C2 real findings.
    agent_write("src/tracksvc/domain/bad.py", "import os\n\n\ndef f(x):\n    return x\n")
    expect_status(common.c1_ruff, Status.FAIL, "C1")
    expect_status(common.c2_mypy, Status.FAIL, "C2")
    agent_write("src/tracksvc/domain/bad.py", "")
    notes.append("C1/C2 findings")

    # C1: a public function without a docstring is the only finding.
    agent_write("src/tracksvc/domain/bad.py", "def double(x: int) -> int:\n    return x * 2\n")
    status, evidence = status_of(common.c1_ruff)
    expect(status is Status.FAIL and "D103" in evidence, f"C1 missing docstring: {status} {evidence}")
    expect_status(common.c2_mypy, Status.PASS, "C2")
    agent_write("src/tracksvc/domain/bad.py", "")
    notes.append("C1 missing docstring")

    # C7: relaxed configs are detected and do not help.
    agent_write("ruff.toml", "[lint]\nselect = []\n")
    agent_write("mypy.ini", "[mypy]\nignore_errors = True\n")
    agent_write("src/tracksvc/domain/bad.py", "import os\n\n\ndef f(x):\n    return x\n")
    expect_status(common.c7_configs_unchanged, Status.FAIL, "C7")
    status, evidence = status_of(common.c1_ruff)
    expect(status is Status.FAIL and "baseline copy used" in evidence, f"C1 with modified config: {evidence}")
    status, evidence = status_of(common.c2_mypy)
    expect(status is Status.FAIL and "baseline copy used" in evidence, f"C2 with modified config: {evidence}")
    notes.append("C7 modified configs")
    return "; ".join(notes)


RUFF_OFF = "exclude = [\"*\"]\n\n[lint]\nselect = []\n"
MYPY_OFF = "[mypy]\nignore_errors = True\n\n[mypy-tracksvc.*]\nignore_errors = True\n"
PYPROJECT_OFF = (
    "\n[tool.ruff]\nexclude = [\"src\", \"*.py\"]\n\n[tool.ruff.lint]\nselect = []\nignore = [\"ALL\"]\n\n"
    "[tool.mypy]\nignore_errors = true\nexclude = [\"src\"]\n"
)
# Config files a tool would discover on its own, all of them switching it off.
HOSTILE_CONFIGS = {
    "src/ruff.toml": RUFF_OFF,
    "src/tracksvc/.ruff.toml": RUFF_OFF,
    "src/tracksvc/domain/ruff.toml": RUFF_OFF,
    "src/tracksvc/pyproject.toml": PYPROJECT_OFF,
    "src/tracksvc/domain/pyproject.toml": PYPROJECT_OFF,
    "setup.cfg": MYPY_OFF + "\n[flake8]\nignore = E,F,W\n",
    ".mypy.ini": MYPY_OFF,
    "src/mypy.ini": MYPY_OFF,
    "src/tracksvc/mypy.ini": MYPY_OFF,
    "src/tracksvc/setup.cfg": MYPY_OFF,
}


def s_nested_tool_configs() -> str:
    """Nested ruff.toml, `[tool.ruff]`, `[tool.mypy]` and setup.cfg written by the
    agent change nothing in C1 / C2: the supplied root configs are the only ones."""
    agent_git("checkout", "-q", "--", "ruff.toml", "mypy.ini")
    pyproject = (config.APP_DIR / "pyproject.toml").read_text()
    agent_write("src/tracksvc/domain/bad.py", "import os\n\n\ndef f(x):\n    return x\n")
    try:
        expect_status(common.c7_configs_unchanged, Status.PASS, "C7")
        before = {"C1": status_of(common.c1_ruff), "C2": status_of(common.c2_mypy)}
        for cid, needle in (("C1", "F401"), ("C2", "bad.py")):
            status, text = before[cid]
            expect(status is Status.FAIL and needle in text, f"{cid} before: {status} {text}")
            expect("baseline copy used" not in text, f"{cid} must use the root config: {text}")

        for relative, content in HOSTILE_CONFIGS.items():
            agent_write(relative, content)
        agent_write("pyproject.toml", pyproject + PYPROJECT_OFF)
        expect_status(common.c7_configs_unchanged, Status.PASS, "C7")
        after = {"C1": status_of(common.c1_ruff), "C2": status_of(common.c2_mypy)}
        for cid in ("C1", "C2"):
            expect(after[cid] == before[cid], f"{cid} changed by the agent's configs: {before[cid]} -> {after[cid]}")
    finally:
        for relative in HOSTILE_CONFIGS:
            agent_remove(relative)
        agent_write("pyproject.toml", pyproject)
        agent_write("src/tracksvc/domain/bad.py", "")
    return f"C1 and C2 report the same findings with {len(HOSTILE_CONFIGS) + 1} hostile config files in /app"


# --- part C: step-1 criteria on the fixture service ----------------------------

STEP1_FIXTURES = Path(__file__).resolve().parent.parent / "selftest_fixtures" / "step1"
STEP1_PACKAGES = ("", "domain", "application", "infrastructure", "api")
STEP1_IDS = [f"1.{n}" for n in range(1, 14)]
COMMON_IDS = ["C1", "C2", "C3", "C7", "C8", "C4"]
# Overlay directory -> the step-1 criteria it must break; all others must pass.
STEP1_VARIANTS = {
    "health_needs_db": {"1.6"},
    "ready_always_ok": {"1.7", "1.11", "1.12"},
    "creates_tables": {"1.10", "1.11"},  # its start-up DDL hangs on a silent database
    "hardcoded_migration_url": {"1.2", "1.3"},
    "no_connect_timeout": {"1.11", "1.12"},
    "domain_module": {"1.9"},
    "no_probe_deadline": {"1.12"},  # hangs only once an established connection goes silent
    "error_details_null": {"1.7", "1.11", "1.12"},
    "error_extra_key": {"1.7", "1.11", "1.12"},
    "import_time_config": {"1.13"},
}
# Paths of the base fixture an overlay removes (it supplies `domain.py` instead).
STEP1_REMOVALS = {"domain_module": ["src/tracksvc/domain"]}


def install_step1_fixture(overlay: str | None = None) -> None:
    """Replace /app content with the fixture service, optionally a broken variant."""
    restore_app()
    for package in STEP1_PACKAGES:
        agent_write(str(Path("src") / config.PACKAGE / package / "__init__.py"), "")
    for layer in ("base", overlay):
        if layer is None:
            continue
        root = STEP1_FIXTURES / layer
        files = sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        expect(bool(files), f"fixture directory {root} is empty")
        for path in files:
            agent_write(str(path.relative_to(root)), path.read_text())
    for relative in STEP1_REMOVALS.get(overlay or "", []):
        agent_remove(relative)


def _problems(results: dict[str, tuple[str, str]], wanted: dict[str, str]) -> str:
    return "\n".join(
        f"{cid}: expected {wanted[cid]}, got {status}: {evidence}"
        for cid, (status, evidence) in results.items()
        if status != wanted[cid]
    )


def s_step1_fixture_passes() -> str:
    """Full step-1 run on the fixture service: every scored criterion passes."""
    install_step1_fixture()
    http.OBSERVED.clear()
    state = State().load()
    state.data = {"steps": {}}
    state.save()
    runner.run(1)
    report = json.loads((config.LOG_DIR / "criteria.json").read_text())
    results = {c["id"]: (c["status"], c["evidence"]) for c in report["criteria"]}
    ids = [*COMMON_IDS, *STEP1_IDS, "C6"]
    expect(list(results) == ids, f"criteria: {list(results)}")
    problems = _problems(results, dict.fromkeys(ids, "pass"))
    expect(not problems, problems)
    expect(report["reward"] == 1.0, f"reward: {report['reward']}")
    left = db.admin_sql("SELECT count(*) FROM pg_database WHERE datname LIKE 'tracksvc\\_step1%'")
    expect(left == "0", "step-1 databases were not dropped")
    expect(not proc.agent_pids(), "agent processes left after the run")
    return f"{len(ids)} criteria pass on the fixture service, reward 1.0"


def step1_variant(overlay: str, broken: set[str]):  # type: ignore[no-untyped-def]
    """Step-1 criteria on a broken variant: exactly `broken` fail."""

    def scenario() -> str:
        install_step1_fixture(overlay)
        reset.reset()
        suite = Suite()
        ctx = common.Context(step=1, state=State().load(), work_dir=common.make_work_dir())
        step1.register(suite, ctx)
        results = {r.id: (r.status.value, r.evidence) for r in suite.run()}
        expect(list(results) == STEP1_IDS, f"criteria: {list(results)}")
        wanted = {cid: "fail" if cid in broken else "pass" for cid in STEP1_IDS}
        problems = _problems(results, wanted)
        expect(not problems, problems)
        return f"{', '.join(sorted(broken))} fail, the other {len(STEP1_IDS) - len(broken)} pass"

    return scenario


# --- part D: hostile agent -----------------------------------------------------

# Imported by the start-up hooks and by the agent's code: one line per call.
UID_PROBE = (
    "import os\nimport sys\n\n\ndef record(kind):\n"
    "    argv = getattr(sys, \"argv\", None) or [\"-\"]\n"
    "    ids = [os.getpid(), os.getuid(), os.geteuid(), os.getgid(), os.getegid()]\n"
    "    groups = \",\".join(str(g) for g in os.getgroups()) or \"-\"\n"
    "    with open({log!r}, \"a\") as fh:\n"
    "        fh.write(\"\\t\".join([kind, *map(str, ids), groups, os.path.basename(argv[0])]) + \"\\n\")\n"
)
PROBE_CALL = "import bench_uid_probe\n\nbench_uid_probe.record(\"{kind}\")\n"
# kind -> the command of contract §2.2 whose process must have produced it.
UID_ENTRY_POINTS = {"conftest": "pytest", "alembic-env": "alembic", "app": "uvicorn"}

# Classic daemon: double fork, own session, SIGTERM / SIGHUP ignored, stdio
# detached; answers 200 to everything on the port given as argv[1].
DAEMON = (
    "import os, signal, socket, sys\n"
    "if os.fork():\n    os._exit(0)\n"
    "os.setsid()\n"
    "if os.fork():\n    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "null = os.open(os.devnull, os.O_RDWR)\n"
    "for fd in (0, 1, 2):\n    os.dup2(null, fd)\n"
    "server = socket.create_server((\"0.0.0.0\", int(sys.argv[1])))\n"
    "open(sys.argv[2], \"w\").write(str(os.getpid()))\n"
    "while True:\n"
    "    conn, _ = server.accept()\n"
    "    conn.sendall(b\"HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\nContent-Length: 19\\r\\n\\r\\n\" + b'{\"status\":\"daemon\"}')\n"
    "    conn.close()\n"
)
SNAPSHOT_BIN = "/usr/local/bin/bench-snapshot"
SNAPSHOT_OUT = Path("/logs/artifacts")


def start_daemon(work: Path) -> int:
    """Start the daemon as the agent user; returns its PID once it holds :8000."""
    pidfile = work / "daemon.pid"
    pidfile.unlink(missing_ok=True)
    agent_python(DAEMON, str(config.HTTP_PORT), str(pidfile))
    expect(proc.wait_until(lambda: pidfile.exists() and proc.port_open(config.HTTP_PORT), 10.0), "daemon did not start")
    pid = int(pidfile.read_text())
    stat = proc._proc_stat(pid)
    expect(pid in proc.agent_pids() and stat is not None, f"daemon {pid} is not an agent process")
    expect(stat is not None and stat[1] not in proc.agent_pids(), "daemon was not reparented (no double fork)")
    return pid


def run_snapshot(label: str) -> dict[str, Any]:
    """Run the collect hook as root, like Harbor does; undo its commit."""
    head = agent_git("rev-parse", "HEAD")
    # In a trial this is a Harbor mount the agent user can write the bundle to.
    SNAPSHOT_OUT.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_OUT.chmod(0o777)
    try:
        result = subprocess.run([SNAPSHOT_BIN, label], capture_output=True, text=True, timeout=120, check=False)
        meta = dict(json.loads((SNAPSHOT_OUT / f"snapshot-{label}.json").read_text()))
        expect(result.returncode == 0, f"bench-snapshot failed: {result.stdout}{result.stderr}{meta}")
        expect(meta["status"] == "ok" and meta["commit"] != head, f"snapshot metadata: {meta}")
        return meta
    finally:
        # The repository config may still name hostile helpers (snapshot-git-config).
        quiet = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
        proc.run_as_agent(["git", *quiet, "reset", "-q", "--mixed", head])


def s_double_fork_daemon() -> str:
    """An agent daemon that holds :8000 is gone after the snapshot hook and
    after the verifier's own clean-up, whichever runs first."""
    work = common.make_work_dir()
    pid = start_daemon(work)
    stopped = proc.stop_agent_processes()  # SIGTERM is ignored: the SIGKILL path
    expect(stopped >= 1 and pid not in proc.agent_pids(), "daemon survived stop_agent_processes")
    expect(not proc.port_open(config.HTTP_PORT), "port 8000 still held")

    pid = start_daemon(work)
    run_snapshot("selftest-daemon")
    expect(pid not in proc.agent_pids() and not proc.agent_pids(), "daemon survived bench-snapshot")
    expect(not proc.port_open(config.HTTP_PORT), "port 8000 still held after bench-snapshot")
    return "double-forked daemon on :8000 killed by stop_agent_processes and by bench-snapshot"


def s_snapshot_git_config() -> str:
    """Helpers named in the agent's git config (fsmonitor, hooks) do not run
    during the snapshot commit."""
    work = common.make_work_dir()
    marker = work / "git-helper.log"
    helper = ".git/bench-helper.sh"
    hooks = ".git/bench-hooks"
    script = f"#!/bin/sh\necho \"$0 uid=$(id -u)\" >> {marker}\n"
    for relative in (helper, f"{hooks}/pre-commit", f"{hooks}/post-commit"):
        agent_write(relative, script)
        proc.run_as_agent(["chmod", "0755", str(config.APP_DIR / relative)])
    try:
        agent_git("config", "core.fsmonitor", str(config.APP_DIR / helper))
        agent_git("config", "core.hooksPath", str(config.APP_DIR / hooks))
        agent_git("config", "core.untrackedCache", "true")
        # The trap is live: a plain `git status` of the agent runs the helper.
        agent_git("status", "--porcelain")
        expect(marker.exists() and "bench-helper.sh" in marker.read_text(), "the fsmonitor helper is not wired")
        marker.unlink()
        agent_write("snapshot-probe.txt", "x\n")
        meta = run_snapshot("selftest-gitconfig")
        expect(not marker.exists(), f"git helper ran during the snapshot: {marker.read_text() if marker.exists() else ''}")
    finally:
        for key in ("core.fsmonitor", "core.hooksPath", "core.untrackedCache"):
            proc.run_as_agent(["git", "config", "--unset", key])
        agent_remove(helper)
        agent_remove(hooks)
        agent_remove("snapshot-probe.txt")
    return f"snapshot {meta['commit'][:8]} committed; fsmonitor and hooks from the agent's config did not run"


def s_snapshot_fallback() -> str:
    """The agent broke `.git` (rewrote every object file): no bundle can be
    made, the snapshot falls back to an archive of the working copy."""
    work = common.make_work_dir()
    backup = work / "git-backup"
    label = "selftest-fallback"
    agent_write("fallback-probe.txt", "kept\n")
    agent_write("src/fallback_probe.py", "X = 1\n")
    expect(proc.run_as_agent(["cp", "-a", str(config.APP_DIR / ".git"), str(backup)]).returncode == 0, "cannot back up .git")
    SNAPSHOT_OUT.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_OUT.chmod(0o777)
    try:
        # What `find . -type f -exec sed -i ... {} +` does to the object store.
        broken = proc.run_as_agent(
            ["sh", "-c", "find .git/objects -type f -exec chmod u+w {} + && find .git/objects -type f -exec sh -c 'printf x >> \"$1\"' _ {} \\;"]
        )
        expect(broken.returncode == 0, f"cannot corrupt .git: {broken.output}")
        result = subprocess.run([SNAPSHOT_BIN, label], capture_output=True, text=True, timeout=120, check=False)
        meta = dict(json.loads((SNAPSHOT_OUT / f"snapshot-{label}.json").read_text()))
        expect(result.returncode == 0, f"a fallback snapshot must not fail the hook: {result.stdout}{result.stderr}{meta}")
        expect(meta["status"] == "fallback" and meta["error"], f"snapshot metadata: {meta}")
        expect(meta["archive"] == f"snapshot-{label}.tar.gz" and meta["bundle"] is None, f"snapshot files in metadata: {meta}")
        expect(not (SNAPSHOT_OUT / f"snapshot-{label}.bundle").exists(), "a broken bundle was left behind")
        listing = subprocess.run(
            ["tar", "-tzf", str(SNAPSHOT_OUT / meta["archive"])], capture_output=True, text=True, check=False
        )
        names = set(listing.stdout.split())
        expect(listing.returncode == 0 and {"./fallback-probe.txt", "./src/fallback_probe.py", "./pyproject.toml"} <= names,
               f"archive content: {sorted(names)[:20]}")
        expect(not any(n.startswith(("./.venv/", "./.git/")) for n in names), "archive contains .venv or .git")
    finally:
        proc.run_as_agent(["rm", "-rf", str(config.APP_DIR / ".git")])
        restored = proc.run_as_agent(["cp", "-a", str(backup), str(config.APP_DIR / ".git")])
        expect(restored.returncode == 0, f"cannot restore .git: {restored.output}")
        agent_remove("fallback-probe.txt")
        agent_remove("src/fallback_probe.py")
    expect(proc.run_as_agent(["git", "fsck", "--no-progress"]).returncode == 0, ".git was not restored")
    return f"broken .git: status fallback ({meta['error']}), archive of {len(names)} entries without .venv and .git"


def s_agent_code_uid() -> str:
    """Full step-1 run on the fixture service with start-up hooks in `.venv`
    and a daemon on :8000: everything the agent wrote runs as `agent`, and the
    daemon is gone before the checks."""
    install_step1_fixture()
    work = common.make_work_dir()
    log = work / "uid.log"
    log.touch()
    log.chmod(0o666)  # a process running as root could write here as well
    site = next((config.APP_DIR / ".venv" / "lib").glob("python*/site-packages"))
    hooks = {
        "bench_uid_probe.py": UID_PROBE.format(log=str(log)),
        "sitecustomize.py": PROBE_CALL.format(kind="sitecustomize"),
        "bench_uid_probe.pth": "import bench_uid_probe; bench_uid_probe.record('pth')\n",
    }
    expect(not any((site / name).exists() for name in hooks), f"{site} already has start-up hooks")
    base = STEP1_FIXTURES / "base"
    try:
        for name, content in hooks.items():
            agent_write(str(site / name), content)
        agent_write("tests/conftest.py", PROBE_CALL.format(kind="conftest"))
        for relative, kind in (("migrations/env.py", "alembic-env"), ("src/tracksvc/api/app.py", "app")):
            agent_write(relative, (base / relative).read_text() + "\n" + PROBE_CALL.format(kind=kind))
        daemon = start_daemon(work)

        http.OBSERVED.clear()
        state = State().load()
        state.data = {"steps": {}}
        state.save()
        runner.run(1)

        expect(daemon not in proc.agent_pids(), "daemon survived the verifier run")
        report = json.loads((config.LOG_DIR / "criteria.json").read_text())
        results = {c["id"]: (c["status"], c["evidence"]) for c in report["criteria"]}
        # The daemon answers every request with its own body: the step-1
        # criteria pass only if the real service got the port.
        ids = ["C4", *STEP1_IDS]
        problems = _problems({cid: results[cid] for cid in ids}, dict.fromkeys(ids, "pass"))
        expect(not problems, problems)

        uid, gid = proc._agent_ids()
        records = [line.split("\t") for line in log.read_text().splitlines()]
        foreign = [r for r in records if r[2:6] != [str(uid), str(uid), str(gid), str(gid)] or r[6] not in ("-", str(gid))]
        expect(not foreign, f"agent code ran with other credentials: {foreign[:5]}")
        pids: dict[str, set[str]] = {}
        for record in records:
            pids.setdefault(record[0], set()).add(record[1])
            pids.setdefault(f"{record[0]}@{record[7]}", set()).add(record[1])
        notes = []
        for kind, command in UID_ENTRY_POINTS.items():
            entry = pids.get(f"{kind}@{command}", set())
            expect(bool(entry), f"no record of {kind} under {command}: {sorted(pids)}")
            hooked = entry & pids.get("sitecustomize", set()) & pids.get("pth", set())
            expect(bool(hooked), f"start-up hooks did not run in the {command} process")
            notes.append(command)
    finally:
        proc.stop_agent_processes()
        for name in hooks:
            proc.run_as_agent(["rm", "-f", str(site / name)])
    return (
        f"{len(records)} records, all uid={uid} gid={gid}: sitecustomize and .pth of .venv, "
        f"{', '.join(notes)}; daemon on :8000 killed before the checks"
    )


def s_runner_and_state() -> str:
    """Full step runs on the restored empty /app: outputs, state, regressions."""
    restore_app()
    http.OBSERVED.clear()
    state = State().load()
    state.data = {"steps": {}}
    state.save()
    runner.run(1)
    report = json.loads((config.LOG_DIR / "criteria.json").read_text())
    reward = json.loads((config.LOG_DIR / "reward.json").read_text())
    ids = [c["id"] for c in report["criteria"]]
    expect(ids == [*COMMON_IDS, *STEP1_IDS, "C6"], f"criteria: {ids}")
    expect(report["cj1_evidence"] is None, "CJ1 evidence reported at step 1")
    statuses = {c["id"]: c["status"] for c in report["criteria"]}
    expect(statuses["C6"] == "skipped" and not report["criteria"][-1]["scored"], f"C6: {statuses}")
    errors = [c for c in report["criteria"] if c["status"] == "error"]
    expect(not errors, f"criteria in error: {errors}")
    # No service on the empty /app: every step-1 criterion fails, none errors.
    step1_statuses = {statuses[cid] for cid in STEP1_IDS}
    expect(step1_statuses == {"fail"}, f"step-1 criteria on the empty /app: {statuses}")
    scored = len(COMMON_IDS) + len(STEP1_IDS)
    expect(set(reward) == {"reward"} and 0.0 <= reward["reward"] <= 1.0, f"reward.json: {reward}")
    expect(reward["reward"] == report["reward"] == round(2 / scored, 6), f"reward: {reward}")

    # Regression metric: break C1 before "step 2".
    agent_write("bad.py", "import os\n")
    runner.run(2)
    report2 = json.loads((config.LOG_DIR / "criteria.json").read_text())
    expect(report2["regressions"] == {"count": 1, "ids": ["C1"]}, f"regressions: {report2['regressions']}")
    cj1 = report2["criteria"][-1]
    expect(
        (cj1["id"], cj1["status"], cj1["scored"]) == ("CJ1", "evidence", False), f"CJ1 entry: {cj1}"
    )
    expect(report2["counts"]["evidence"] == 1, f"counts: {report2['counts']}")
    expect(report2["cj1_evidence"]["missing_node_ids"] == [], f"CJ1 data: {report2['cj1_evidence']}")
    expect((config.LOG_DIR / evidence.OUTPUT_NAME).is_file(), "cj1-evidence.json not written")
    scored2 = scored + len(step2.IDS)  # step 2 adds its own criteria; all fail on the empty /app
    expect(report2["reward"] == round(1 / scored2, 6), f"CJ1 must not affect the reward: {report2['reward']}")
    stored = json.loads(config.STATE_DIR.joinpath("state.json").read_text())
    expect(set(stored["steps"]) == {"1", "2"}, "state not recorded for both steps")
    expect(oct(config.STATE_DIR.stat().st_mode)[-3:] == "700", "state dir must be 0700")
    restore_app()
    return f"reward step 1 = {reward['reward']}; regression metric and state persistence work"


def main() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    suite = Suite()
    suite.add("infrastructure", s_infrastructure)
    suite.add("identity-and-isolation", s_identity_and_isolation)
    suite.add("process-control", s_process_control)
    suite.add("database", s_database)
    suite.add("reset-empty-app", s_reset_empty_app)
    suite.add("broker", s_broker)
    suite.add("toxiproxy", s_toxiproxy)
    suite.add("http-app-on-empty-app", s_http_app_on_empty_app)
    suite.add("common-on-empty-app", s_common_on_empty_app)
    suite.add("fixture-passes", s_fixture_passes)
    suite.add("fixture-http", s_fixture_http)
    suite.add("c4-semantics", s_c4_semantics)
    suite.add("c4-pytest-env", s_c4_pytest_env)
    suite.add("cj1-evidence", s_cj1_evidence)
    suite.add("cj1-incomplete", s_cj1_incomplete)
    suite.add("fixture-violations", s_fixture_violations)
    suite.add("nested-tool-configs", s_nested_tool_configs)
    suite.add("step1-fixture-passes", s_step1_fixture_passes)
    for overlay, broken in STEP1_VARIANTS.items():
        suite.add(f"step1-variant-{overlay}", step1_variant(overlay, broken))
    for step in (2, 3, 4, 5):  # step self-tests live in their own modules, which import this one
        importlib.import_module(f"benchverify.selftest_step{step}").register(suite)
    suite.add("double-fork-daemon", s_double_fork_daemon)
    suite.add("snapshot-git-config", s_snapshot_git_config)
    suite.add("snapshot-fallback", s_snapshot_fallback)
    suite.add("agent-code-uid", s_agent_code_uid)
    suite.add("runner-and-state", s_runner_and_state)
    try:
        results = suite.run()
    finally:
        proc.stop_agent_processes()
        toxi.ensure_proxy()
    print()
    for result in results:
        print(f"{result.status.value.upper():7} {result.id}: {result.evidence}")
    failed = [r for r in results if r.status is not Status.PASS]
    print(f"\nself-test: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
