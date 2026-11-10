"""pytest plugin loaded by the verifier for criterion C4 (`-p bench_nodeids`).

Writes the node IDs of collected and passed tests to the JSON file named by
the BENCH_NODEIDS_OUT environment variable. Contains nothing secret.
"""

from __future__ import annotations

import json
import os

_collected: list[str] = []
_outcomes: dict[str, str] = {}


def pytest_collection_modifyitems(items):  # type: ignore[no-untyped-def]
    _collected.extend(item.nodeid for item in items)


def pytest_runtest_logreport(report):  # type: ignore[no-untyped-def]
    if report.when == "call":
        _outcomes[report.nodeid] = report.outcome
    elif report.outcome != "passed":
        # setup/teardown failure or skip overrides the call outcome
        _outcomes[report.nodeid] = "skipped" if report.outcome == "skipped" else "error"


def pytest_sessionfinish(session, exitstatus):  # type: ignore[no-untyped-def]
    out = os.environ.get("BENCH_NODEIDS_OUT")
    if not out:
        return
    passed = sorted(n for n, o in _outcomes.items() if o == "passed")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(
            {"collected": sorted(_collected), "passed": passed, "exitstatus": int(exitstatus)},
            fh,
        )
