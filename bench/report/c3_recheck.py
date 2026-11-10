#!/usr/bin/env python3
"""Re-run criterion C3 on a step snapshot, with empty `__init__.py` files added.

Why: neither AGENTS.md nor a task.md asks for `__init__.py`, yet import-linter
does not see a package without one ("Missing layer 'tracksvc.api': module …
does not exist"). Such a C3 failure is a gap of the check, not a layer violation
(rubric layer-boundary rule). The verifier is not changed; the report
re-runs the SAME check on the snapshot:

  - only when C3 failed and every problem it reported is a "Missing layer";
  - the snapshot's `src/` is unpacked into a temporary directory, an empty
    `__init__.py` is added to every directory of the package that has none;
  - `benchverify.common.c3_layers` itself is called (import-linter + AST check),
    nothing of it is copied here.

`c3_layers` needs the verifier toolchain, so it runs in a child process started
with the Python of a virtualenv built from `bench/environment/verifier/requirements.txt`
(`--verifier-python` / `BENCH_VERIFIER_PYTHON`). The import-linter version of
that virtualenv must be the one pinned there, otherwise nothing is rechecked.

    <verifier venv>/bin/python bench/report/c3_recheck.py --app DIR   # child mode
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent.parent
REQUIREMENTS = BENCH_DIR / "environment" / "verifier" / "requirements.txt"
PACKAGE_DIR = "src/tracksvc"
FLAG = "c3_namespace_fix"
_BANNER = re.compile(r"[─-╿▶◀▲▼]")  # the import-linter logo: box-drawing characters and arrows
_MISSING = re.compile(r"Missing layer '[\w.]+': module [\w.]+ does not exist\.")


class RecheckUnavailable(RuntimeError):
    """The recheck cannot be run here (no verifier virtualenv, wrong import-linter)."""


def only_missing_layer(evidence: str) -> bool:
    """True when the C3 evidence consists of import-linter "Missing layer" messages only."""
    lines = [line for line in evidence.splitlines() if not _BANNER.search(line)]
    text = " ".join(" ".join(lines).split()).removeprefix("import-linter:").strip()
    return bool(text) and not _MISSING.sub("", text).strip()


def pinned_import_linter() -> str:
    match = re.search(r"^import-linter==(\S+)", REQUIREMENTS.read_text(), flags=re.MULTILINE)
    if match is None:
        raise RecheckUnavailable(f"import-linter is not pinned in {REQUIREMENTS}")
    return match.group(1)


def add_init_files(app: Path) -> list[str]:
    """Add an empty `__init__.py` to every directory of the package that has none."""
    added: list[str] = []
    package = app / PACKAGE_DIR
    if not package.is_dir():
        return added
    for directory in sorted([package, *(p for p in package.rglob("*") if p.is_dir())]):
        init = directory / "__init__.py"
        if directory.name != "__pycache__" and not init.exists():
            init.touch()
            added.append(init.relative_to(app).as_posix())
    return added


def run_child(verifier_python: Path, app: Path) -> dict[str, Any]:
    """Run `c3_layers` on `app` in the verifier virtualenv."""
    done = subprocess.run(
        [str(verifier_python), str(Path(__file__).resolve()), "--app", str(app)],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        result = dict(json.loads(done.stdout))
    except ValueError as exc:
        raise RecheckUnavailable(f"C3 recheck failed to run: {(done.stderr or done.stdout).strip()[-400:]}") from exc
    if result["import_linter"] != pinned_import_linter():
        raise RecheckUnavailable(
            f"import-linter {result['import_linter']} in {verifier_python}, the verifier pins {pinned_import_linter()}"
        )
    return result


def recheck(snapshot: Any, verifier_python: Path | None, *, add_init: bool = True) -> dict[str, Any]:
    """C3 of an open `snapshots.Snapshot`: {status, evidence, added, import_linter}."""
    if verifier_python is None:
        raise RecheckUnavailable("no verifier virtualenv given (--verifier-python / BENCH_VERIFIER_PYTHON)")
    if not verifier_python.is_file():
        raise RecheckUnavailable(f"no such interpreter: {verifier_python}")
    with tempfile.TemporaryDirectory(prefix="bench-c3-") as tmp:
        app = Path(tmp) / "app"
        app.mkdir()
        snapshot.extract(app, "src/")
        added = add_init_files(app) if add_init else []
        return {**run_child(verifier_python, app), "added": added}


def _child(app: Path) -> dict[str, Any]:
    """Child mode: point the verifier's own C3 at `app` and run it."""
    from importlib.metadata import version

    sys.path.insert(0, str(BENCH_DIR / "tests"))
    from benchverify import common, config, proc
    from benchverify.result import Status, run_check
    from benchverify.state import State

    # The check reads these names when it is called, in this very process.
    config.APP_DIR = app
    config.SRC_DIR = app / "src"
    config.VERIFIER_BIN = Path(sys.executable).parent
    common.PKG_DIR = config.SRC_DIR / config.PACKAGE

    def run_local(
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path = app,
        timeout: float = 300.0,
        unset: Iterable[str] = (),
    ) -> proc.CommandResult:
        # In the container the tool runs as the `agent` user in /app; here the
        # snapshot is a private copy, so the current user and its directory do.
        full_env = {"PATH": "/usr/bin:/bin", "HOME": str(app), "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
        for name in unset:
            full_env.pop(name, None)
        done = subprocess.run(
            list(args), cwd=cwd, env=full_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace", check=False, timeout=timeout,
        )  # fmt: skip
        return proc.CommandResult(list(args), done.returncode, done.stdout or "", 0.0)

    proc.run_as_agent = run_local
    with tempfile.TemporaryDirectory(prefix="bench-c3-work-") as work:
        ctx = common.Context(step=0, state=State(Path(work)), work_dir=Path(work))  # C3 does not read the state
        status, evidence = run_check(lambda: common.c3_layers(ctx))
    return {"status": str(Status(status).value), "evidence": evidence, "import_linter": version("import-linter")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", type=Path, required=True, help="unpacked working copy (child mode)")
    args = parser.parse_args(argv)
    print(json.dumps(_child(args.app.resolve())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
