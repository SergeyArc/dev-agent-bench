"""Self-test of the step-5 criteria (criteria/step5.py).

Registered in the framework self-test (`selftest.main`) and runnable on its own
in a throwaway container of the base image:

    PYTHONPATH=/tests-selftest /opt/verifier/bin/python -m benchverify.selftest_step5 [scenario …]

`step5-topology-check` proves the §7.1 comparison on objects the verifier
declares itself. `step5-fixture-passes` is a full step-5 run (`runner.run(5)`,
cascade included) on the fixture service of `selftest_fixtures/step5`: every
scored criterion passes. Each `step5-variant-*` scenario installs the fixture
with one fault patched into its worker and runs the criteria the fault must
break plus a few that must keep passing.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from criteria import step5

from . import amqp, common, config, db, http, proc, reset, runner, toxi
from . import selftest as base
from .result import Status, Suite
from .state import State

FIXTURES = Path(__file__).resolve().parent.parent / "selftest_fixtures" / "step5" / "base"
PACKAGES = ("", "domain", "application", "infrastructure", "api")
WORKER = "src/tracksvc/worker.py"
STEP5_IDS = [f"5.{n}" for n in range(1, 20)]
NOT_SCORED = {"C6", "CJ1"}


@dataclass(frozen=True)
class Variant:
    """A fault patched into the fixture worker and what it does to the criteria."""

    patches: tuple[tuple[str, str], ...]  # (text of base worker.py, replacement)
    fail: frozenset[str]  # criteria the fault must break
    keep: frozenset[str]  # criteria that must still pass


VARIANTS = {
    # Duplicates are applied again: the idempotency row is written, never consulted.
    "no_dedup": Variant(
        (
            (
                (
                    "                if not await store.record_message(connection, message_id):\n"
                    "                    return None\n"
                ),
                "                await store.record_message(connection, message_id)\n",
            ),
        ),
        frozenset({"5.5", "5.6"}),
        frozenset({"5.3", "5.4", "5.13"}),
    ),
    # One attempt too many: MAX + 1 republished copies, dead copy with x-retry-count MAX + 1.
    "retry_off_by_one": Variant(
        (("retries < self._max_retries", "retries <= self._max_retries"),),
        frozenset({"5.7", "5.8", "5.9", "5.10"}),
        frozenset({"5.11", "5.12"}),
    ),
    # No reconnect: the process ends when the broker connection is lost.
    "exits_on_disconnect": Variant(
        (
            (
                '    connection = await aio_pika.connect_robust(os.environ["RABBITMQ_URL"])\n',
                (
                    '    connection = await aio_pika.connect(os.environ["RABBITMQ_URL"])\n'
                    "    connection.close_callbacks.add(lambda *_: stop.set())\n"
                ),
            ),
        ),
        frozenset({"5.15", "5.16"}),
        frozenset({"5.3", "5.17"}),
    ),
    # The work queue is declared with an argument.
    "queue_arguments": Variant(
        (
            (
                "declare_queue(WORK_QUEUE, durable=True)",
                'declare_queue(WORK_QUEUE, durable=True, arguments={"x-message-ttl": 600000})',
            ),
        ),
        frozenset({"5.1", "5.2"}),
        frozenset({"5.3"}),
    ),
    # The header and the body are validated before the duplicate check: a processed
    # message_id with an invalid body is dead-lettered instead of acknowledged.
    "validation_before_duplicate": Variant(
        (
            (
                (
                    "                if not await store.record_message(connection, message_id):\n"
                    "                    return None\n"
                    "                command = None if _retries(message) is None else _command(message.body)\n"
                    "                if command is None:\n"
                    "                    raise DomainError(INVALID)\n"
                ),
                (
                    "                command = None if _retries(message) is None else _command(message.body)\n"
                    "                if command is None:\n"
                    "                    raise DomainError(INVALID)\n"
                    "                if not await store.record_message(connection, message_id):\n"
                    "                    return None\n"
                ),
            ),
        ),
        frozenset({"5.19"}),
        frozenset({"5.5", "5.14"}),
    ),
    # A bad header or body is retried as a handler failure instead of dead-lettered at once.
    "invalid_retried": Variant(
        (("raise DomainError(INVALID)", 'raise DomainError("internal_error")'),),
        frozenset({"5.14"}),
        frozenset({"5.13"}),
    ),
}


def install_fixture(patches: tuple[tuple[str, str], ...] = ()) -> None:
    """Replace /app content with the fixture service, optionally with a patched worker."""
    base.restore_app()
    for package in PACKAGES:
        base.agent_write(str(Path("src") / config.PACKAGE / package / "__init__.py"), "")
    files = sorted(p for p in FIXTURES.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    base.expect(bool(files), f"fixture directory {FIXTURES} is empty")
    for path in files:
        relative = str(path.relative_to(FIXTURES))
        content = path.read_text()
        if relative == WORKER:
            for old, new in patches:
                base.expect(content.count(old) == 1, f"patch target not found exactly once: {old!r}")
                content = content.replace(old, new)
        base.agent_write(relative, content)


def _expect_clean() -> None:
    """Nothing of a step-5 run is left behind."""
    left = db.admin_sql(f"SELECT count(*) FROM pg_database WHERE datname = '{step5.DATABASE}'")
    base.expect(left == "0", "the step-5 database was not dropped")
    base.expect(not proc.agent_pids(), "agent processes left after the run")
    base.expect(toxi.is_enabled(), "the broker proxy was left disabled")
    base.expect(proc.port_open(config.AMQP_PORT), "the broker was left stopped")
    queues = amqp.all_queues()
    stray = [q for q in queues if q.startswith(amqp.TAP_PREFIX) or q in (config.WORK_QUEUE, config.DEAD_QUEUE)]
    base.expect(not stray, f"queues left behind: {stray}")


def s_topology_check() -> str:
    """The §7.1 comparison accepts the verifier's own declaration and names every difference."""
    amqp.delete_topology()
    missing = step5._topology_problems()
    base.expect(len(missing) == 3, f"empty broker: {missing}")
    amqp.declare_topology()
    base.expect(step5._topology_problems() == [], f"contract topology: {step5._topology_problems()}")
    with amqp.channel() as ch:
        ch.queue_bind(config.DEAD_QUEUE, config.EXCHANGE, routing_key="other")
        ch.queue_unbind(config.WORK_QUEUE, config.EXCHANGE, routing_key=config.ROUTING_KEY)
    unbound = step5._topology_problems()
    base.expect(len(unbound) == 2 and "not bound" in unbound[0] and "has bindings" in unbound[1], f"{unbound}")
    amqp.delete_topology()
    with amqp.channel() as ch:
        ch.exchange_declare(config.EXCHANGE, exchange_type="fanout", durable=False, auto_delete=True)
        ch.queue_declare(config.WORK_QUEUE, durable=True, arguments={"x-message-ttl": 600000})
        ch.queue_bind(config.WORK_QUEUE, config.EXCHANGE, routing_key=config.ROUTING_KEY)
        ch.queue_declare(config.DEAD_QUEUE, durable=False, auto_delete=True)
        ch.queue_declare(config.DEAD_QUEUE + ".quorum", durable=True, arguments={"x-queue-type": "quorum"})
    wrong = "\n".join(step5._topology_problems())
    quorum = [
        row["arguments"]
        for row in step5._ctl_json("list_queues", "--formatter", "json", "name", "arguments")
        if row["name"].endswith(".quorum")
    ]
    with amqp.channel() as ch:
        ch.queue_delete(config.DEAD_QUEUE + ".quorum")
    amqp.delete_topology()
    for needle in (
        "type is 'fanout'", f"exchange `{config.EXCHANGE}`: durable is False", "auto_delete is True",
        "x-message-ttl", f"queue `{config.DEAD_QUEUE}`: durable is False",
    ):
        base.expect(needle in wrong, f"difference {needle!r} not reported:\n{wrong}")
    base.expect(step5._arguments(quorum[0]) == {"x-queue-type": "quorum"}, f"quorum queue arguments: {quorum}")
    return "contract topology accepted; missing objects, wrong properties, arguments and bindings reported"


def s_fixture_passes() -> str:
    """Full step-5 run on the fixture service: every scored criterion passes."""
    install_fixture()
    http.OBSERVED.clear()
    state = State().load()
    state.data = {"steps": {}}
    state.save()
    runner.run(5)
    report = json.loads((config.LOG_DIR / "criteria.json").read_text())
    results = {c["id"]: (c["status"], c["evidence"]) for c in report["criteria"]}
    present = [cid for cid in results if cid.startswith("5.")]
    base.expect(present == STEP5_IDS, f"step-5 criteria: {present}")
    scored = [cid for cid in results if cid not in NOT_SCORED]
    problems = base._problems({cid: results[cid] for cid in [*scored, "C6"]}, dict.fromkeys(results, "pass"))
    base.expect(not problems, problems)
    base.expect(report["reward"] == 1.0, f"reward: {report['reward']}")
    for cid in STEP5_IDS:
        tag = "[required-pass] " if cid in step5.REQUIRED_PASS else next(
            f"[{group}] " for group, members in step5.GROUPS.items() if cid in members
        )
        base.expect(results[cid][1].startswith(tag), f"{cid}: evidence without the tag {tag!r}: {results[cid][1]}")
    _expect_clean()
    (base.LOGS / "step5-criteria.json").write_text(json.dumps(report, indent=2))
    took = {c["id"]: c["duration_sec"] for c in report["criteria"] if c["id"].startswith("5.")}
    return (
        f"{len(scored)} scored criteria pass on the fixture service (cascade: {len(scored) - len(STEP5_IDS)} "
        f"earlier and common), reward 1.0; step-5 checks took {sum(took.values()):.0f} s"
    )


def variant(name: str):  # type: ignore[no-untyped-def]
    """Step-5 criteria on a broken variant: `fail` fail, `keep` pass."""
    spec = VARIANTS[name]

    def scenario() -> str:
        install_fixture(spec.patches)
        reset.ensure_infrastructure()
        full = Suite()
        ctx = common.Context(step=5, state=State().load(), work_dir=common.make_work_dir())
        step5.register(full, ctx)
        wanted = {cid: "fail" for cid in spec.fail} | {cid: "pass" for cid in spec.keep}
        suite = Suite([entry for entry in full.entries if entry.id in wanted])
        results = {r.id: (r.status.value, r.evidence) for r in suite.run()}
        base.expect(set(results) == set(wanted), f"criteria: {sorted(results)}")
        problems = base._problems(results, wanted)
        base.expect(not problems, problems)
        _expect_clean()
        why = "; ".join(f"{cid}: {' / '.join(results[cid][1].splitlines()[:2])[:260]}" for cid in sorted(spec.fail))
        return f"{', '.join(sorted(spec.fail))} fail, {', '.join(sorted(spec.keep))} pass. {why}"

    return scenario


def register(suite: Suite) -> None:
    suite.add("step5-topology-check", s_topology_check)
    suite.add("step5-fixture-passes", s_fixture_passes)
    for name in VARIANTS:
        suite.add(f"step5-variant-{name}", variant(name))


def main(argv: list[str]) -> int:
    base.LOGS.mkdir(parents=True, exist_ok=True)
    reset.ensure_infrastructure()
    full = Suite()
    register(full)
    unknown = sorted(set(argv) - {entry.id for entry in full.entries})
    if unknown:
        print(f"unknown scenario(s): {unknown}; known: {[entry.id for entry in full.entries]}")
        return 2
    suite = Suite([entry for entry in full.entries if not argv or entry.id in argv])
    try:
        results = suite.run()
    finally:
        proc.stop_agent_processes()
        toxi.ensure_proxy()
    print()
    for result in results:
        print(f"{result.status.value.upper():7} {result.id} ({result.duration_sec}s): {result.evidence}")
    failed = [r for r in results if r.status is not Status.PASS]
    print(f"\nstep-5 self-test: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
