"""pytest plugin loaded for every test run of the mutation session.

The verifier gives the agent's suite an empty, unmigrated database of its own and stops
every agent process afterwards (C4). A mutation session runs the suite once per mutant
against the same container, so both steps are repeated here before each run: processes
orphaned by the previous run are killed and the pytest database is recreated.

This happens before the project's conftest files are loaded: a suite may migrate its
database in its own `pytest_configure` (or at import), and that must come after the reset.

Nothing of the project under test is imported or changed.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

PYTEST_DB = "tracksvc_pytest"
ADMIN_DSN = "postgresql://tracksvc:tracksvc@127.0.0.1:5432/tracksvc"


def _kill_orphans() -> int:
    """SIGKILL processes of this user that were reparented to PID 1."""
    me = os.getuid()
    killed = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != me:
                continue
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) != 1:  # ppid
                continue
            os.kill(int(entry.name), signal.SIGKILL)
            killed += 1
        except (OSError, ValueError, IndexError):
            continue
    return killed


def _psql(statement: str) -> None:
    subprocess.run(
        ["psql", "-qtAX", "-v", "ON_ERROR_STOP=1", ADMIN_DSN, "-c", statement],
        check=True,
        capture_output=True,
        timeout=60,
    )


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests() -> None:
    if os.environ.get("BENCHMUT_RESET") != "1":
        return
    killed = _kill_orphans()
    _psql(f'DROP DATABASE IF EXISTS "{PYTEST_DB}" WITH (FORCE)')
    _psql(f'CREATE DATABASE "{PYTEST_DB}" OWNER tracksvc')
    log = os.environ.get("BENCHMUT_RESET_LOG")
    if log and killed:
        with open(log, "a") as handle:
            handle.write(
                f"{os.environ.get('MUTANT_UNDER_TEST', '')}\torphans_killed={killed}\n"
            )
