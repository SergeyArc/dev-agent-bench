"""Common criteria C1–C8 of contract §9 as reusable checks.

Every function is a `Check` (see result.py): it returns evidence on success,
raises CheckFailed / CheckSkipped otherwise. C5 is an execution rule, not a
check: see runner.py (cascade) and state.regressions (metric).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db, http, proc
from .result import CheckFailed, CheckSkipped, Suite
from .state import State

PKG_DIR = config.SRC_DIR / config.PACKAGE
INNER_LAYERS = ("domain", "application")


@dataclass
class Context:
    """What the common checks need to know about the current run."""

    step: int
    state: State
    work_dir: Path  # scratch directory, readable and writable by the agent user
    pytest_env: dict[str, str] = field(default_factory=dict)  # extra variables for C4
    # Filled by C4 and stored in the state: passed IDs of a green run, and the
    # collected IDs of any run that got as far as collection (CJ1 evidence).
    pytest_passed: list[str] | None = None
    pytest_collected: list[str] | None = None


def make_work_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="bench-verify-"))
    shutil.chown(path, config.AGENT_USER, config.AGENT_USER)
    path.chmod(0o755)
    return path


# --- config integrity (C7) and tool configs ----------------------------------


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _config_intact(name: str) -> bool:
    baseline = _sha256(config.BASELINE_DIR / name)
    return baseline is not None and _sha256(config.APP_DIR / name) == baseline


def _tool_config(name: str) -> tuple[Path, str]:
    """Config file a tool must be run with, plus a note for the evidence.

    Normally `/app/<name>` (byte-identical to the supplied one). If the agent
    changed it, the pristine baseline copy is used instead: C1/C2 are always
    evaluated against the supplied configuration, and C7 reports the change.
    """
    if _config_intact(name):
        return config.APP_DIR / name, ""
    return config.BASELINE_DIR / name, f"[{name} was modified; baseline copy used] "


def c7_configs_unchanged() -> str:
    changed = [name for name in ("ruff.toml", "mypy.ini") if not _config_intact(name)]
    if changed:
        details = ", ".join(
            f"{name}: sha256 {_sha256(config.APP_DIR / name) or 'missing'} != baseline "
            f"{_sha256(config.BASELINE_DIR / name)}"
            for name in changed
        )
        raise CheckFailed(f"modified or missing: {details}")
    return "ruff.toml and mypy.ini are byte-identical to the initial commit"


# --- C1 ruff, C2 mypy ---------------------------------------------------------


def c1_ruff() -> str:
    cfg, note = _tool_config("ruff.toml")
    result = proc.run_as_agent(
        [
            str(config.VERIFIER_BIN / "ruff"), "check", "--config", str(cfg), "--no-cache",
            "--output-format", "concise", str(config.APP_DIR),
        ],
        timeout=180,
    )
    if result.timed_out:
        raise RuntimeError("ruff timed out")
    if result.returncode == 0:
        return note + "ruff check: zero violations"
    if result.returncode == 1:
        raise CheckFailed(note + result.tail(25))
    raise RuntimeError(f"ruff exited with {result.returncode}:\n{result.tail(25)}")


def c2_mypy() -> str:
    cfg, note = _tool_config("mypy.ini")
    if not PKG_DIR.is_dir():
        raise CheckFailed(f"package {config.PACKAGE} not found at {PKG_DIR}")
    result = proc.run_as_agent(
        [
            str(config.VERIFIER_BIN / "mypy"), "--config-file", str(cfg), "--strict",
            "--python-executable", str(config.APP_DIR / ".venv/bin/python"),
            "--cache-dir", os.devnull, "--no-color-output", "-p", config.PACKAGE,
        ],
        timeout=600,
    )
    if result.timed_out:
        raise RuntimeError("mypy timed out")
    if result.returncode == 0:
        return note + result.tail(1)
    if result.returncode in (1, 2):
        raise CheckFailed(note + result.tail(25))
    raise RuntimeError(f"mypy exited with {result.returncode}:\n{result.tail(25)}")


# --- C3 layer rule: import-linter + AST check, C8 dynamic imports ------------


def _layer_exists(name: str) -> bool:
    return (PKG_DIR / name).is_dir() or (PKG_DIR / f"{name}.py").is_file()


def _import_linter_config() -> str:
    """Layer contracts of contract §2.3 for the layers that exist. The
    import-linter configuration lives only in the verifier."""
    top = [f"{config.PACKAGE}.{n}" for n in ("api", "worker") if _layer_exists(n)]
    layers = [" : ".join(top)] if top else []
    layers += [f"{config.PACKAGE}.{n}" for n in ("infrastructure", "application", "domain") if _layer_exists(n)]
    body = "\n".join(f"    {layer}" for layer in layers)
    return (
        "[importlinter]\n"
        f"root_package = {config.PACKAGE}\n"
        "include_external_packages = True\n\n"
        "[importlinter:contract:layers]\n"
        "name = Layer rule (contract 2.3)\n"
        "type = layers\n"
        f"layers =\n{body}\n"
    )


def _iter_layer_files(layer: str) -> Iterator[tuple[str, Path, bool]]:
    """(module name, path, is_package) for every module of an inner layer."""
    single = PKG_DIR / f"{layer}.py"
    if single.is_file():
        yield f"{config.PACKAGE}.{layer}", single, False
    root = PKG_DIR / layer
    if root.is_dir():
        for path in sorted(root.rglob("*.py")):
            parts = list(path.relative_to(config.SRC_DIR).with_suffix("").parts)
            is_package = parts[-1] == "__init__"
            if is_package:
                parts.pop()
            yield ".".join(parts), path, is_package


def _resolve_from(module: str, is_package: bool, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = module.split(".") if is_package else module.split(".")[:-1]
    base = package[: len(package) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _allowed(target: str, layer: str) -> bool:
    top = target.split(".")[0]
    if top != config.PACKAGE:
        return top in sys.stdlib_module_names or top in config.FOUNDATION_ALLOWLIST
    allowed = ("domain",) if layer == "domain" else ("domain", "application")
    return any(
        target == f"{config.PACKAGE}.{name}" or target.startswith(f"{config.PACKAGE}.{name}.")
        for name in allowed
    )


def _ast_violations(layer: str) -> list[str]:
    """Imports of an inner layer outside {itself, lower layers, stdlib, allowlist}.

    All imports count, including those under `if TYPE_CHECKING:` and inside
    functions.
    """
    found: list[str] = []
    for module, path, is_package in _iter_layer_files(layer):
        rel = path.relative_to(config.APP_DIR)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            found.append(f"{rel}: cannot parse ({exc})")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = _resolve_from(module, is_package, node)
                # `from tracksvc import infrastructure` imports a submodule:
                # accept the statement only if the base or base.name is allowed.
                targets = [
                    base if _allowed(base, layer) else f"{base}.{alias.name}".strip(".")
                    for alias in node.names
                ]
            else:
                continue
            found += [
                f"{rel}:{node.lineno}: {layer} imports {target}"
                for target in targets
                if not _allowed(target, layer)
            ]
    return found


def c3_layers(ctx: Context) -> str:
    if not PKG_DIR.is_dir():
        raise CheckFailed(f"package {config.PACKAGE} not found at {PKG_DIR}")
    problems: list[str] = []

    cfg = ctx.work_dir / "importlinter.ini"
    cfg.write_text(_import_linter_config())
    result = proc.run_as_agent(
        [str(config.VERIFIER_BIN / "lint-imports"), "--config", str(cfg), "--no-cache"],
        env={"PYTHONPATH": str(config.SRC_DIR)},
        timeout=180,
    )
    if result.timed_out:
        raise RuntimeError("import-linter timed out")
    if result.returncode != 0:
        problems.append("import-linter:\n" + result.tail(25))

    for layer in INNER_LAYERS:
        problems += _ast_violations(layer)

    if problems:
        raise CheckFailed("\n".join(problems))
    missing = [n for n in config.LAYERS if not _layer_exists(n)]
    note = f" (layers absent: {', '.join(missing)})" if missing else ""
    return "import-linter contracts kept; domain/application import only allowed modules" + note


def c8_no_dynamic_imports() -> str:
    if not any(_layer_exists(layer) for layer in INNER_LAYERS):
        raise CheckSkipped("neither tracksvc.domain nor tracksvc.application exists")
    found: list[str] = []
    for layer in INNER_LAYERS:
        for _module, path, _ in _iter_layer_files(layer):
            rel = path.relative_to(config.APP_DIR)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError) as exc:
                found.append(f"{rel}: cannot parse ({exc})")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                elif isinstance(node, ast.Name) and node.id == "__import__":
                    found.append(f"{rel}:{node.lineno}: __import__")
                    continue
                elif isinstance(node, ast.Attribute) and node.attr in ("__import__", "import_module"):
                    found.append(f"{rel}:{node.lineno}: .{node.attr}")
                    continue
                else:
                    continue
                found += [
                    f"{rel}:{node.lineno}: import {name}"
                    for name in names
                    if name == "importlib" or name.startswith("importlib.")
                ]
    if found:
        raise CheckFailed("\n".join(found))
    return "no importlib / __import__ in domain and application"


# --- C4 pytest hygiene --------------------------------------------------------

# Variables that change what pytest collects or which plugins it loads. They are
# never passed to the agent's pytest, wherever they come from. The agent's own
# pytest configuration files (pyproject.toml, pytest.ini, …) stay in force.
PYTEST_ENV_BLOCKLIST = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")


def c4_pytest(ctx: Context) -> str:
    """`uv run pytest` exits 0 and runs >= 1 test. Test content is not scored;
    what happened to the suite since the previous step is CJ1 evidence
    (evidence.py), not part of this criterion.

    Environment promised to the agent: DATABASE_URL points to a reachable,
    empty, unmigrated database of its own (never the one the service checks
    use), RABBITMQ_URL to the broker.
    """
    out = ctx.work_dir / "nodeids.json"
    out.unlink(missing_ok=True)
    ctx.pytest_passed = ctx.pytest_collected = None
    env = {
        "DATABASE_URL": config.database_url(config.PYTEST_DB),
        "RABBITMQ_URL": config.rabbitmq_url(),
        **ctx.pytest_env,
        "PYTHONPATH": str(config.PLUGINS_DIR),
        "BENCH_NODEIDS_OUT": str(out),
    }
    db.recreate_database(config.PYTEST_DB)
    try:
        result = proc.run_as_agent(
            [*config.PYTEST_CMD, "-p", "bench_nodeids", "-p", "no:cacheprovider"],
            env=env,
            unset=PYTEST_ENV_BLOCKLIST,
            timeout=900,
        )
    finally:
        proc.stop_agent_processes()  # nothing started by the tests may outlive them
        db.drop_database(config.PYTEST_DB)
    if result.timed_out:
        raise CheckFailed("uv run pytest timed out after 900 s")
    try:
        report = json.loads(out.read_text())
        passed = [str(node) for node in report["passed"]]
        ctx.pytest_collected = [str(node) for node in report["collected"]]
    except (OSError, ValueError, KeyError, TypeError):
        passed = []
    if result.returncode != 0:
        raise CheckFailed(f"uv run pytest exited with {result.returncode}\n{result.tail(25)}")
    ctx.pytest_passed = passed  # recorded only for a green run
    if not passed:
        raise CheckFailed("uv run pytest exited 0 but no test passed")
    return f"uv run pytest exited 0; {len(passed)} test(s) passed"


# --- C6 diagnostic ------------------------------------------------------------


def c6_no_http_500() -> str:
    """Diagnostic, no weight: no HTTP 500 among the responses observed so far."""
    if not http.OBSERVED:
        raise CheckSkipped("no HTTP responses were observed during this step's checks")
    bad = [o for o in http.OBSERVED if o.status == 500]
    if bad:
        lines = [f"{o.method} {o.url} -> 500" for o in bad[:20]]
        raise CheckFailed(f"{len(bad)} response(s) with status 500:\n" + "\n".join(lines))
    return f"{len(http.OBSERVED)} HTTP responses observed, none with status 500"


# --- registration -------------------------------------------------------------


def register_static(suite: Suite, ctx: Context) -> None:
    """Checks that only read the working copy: run before any reset."""
    suite.add("C1", c1_ruff)
    suite.add("C2", c2_mypy)
    suite.add("C3", lambda: c3_layers(ctx))
    suite.add("C7", c7_configs_unchanged)
    suite.add("C8", c8_no_dynamic_imports)


def register_pytest(suite: Suite, ctx: Context) -> None:
    suite.add("C4", lambda: c4_pytest(ctx))


def register_diagnostics(suite: Suite) -> None:
    """Must be registered last: C6 looks at every response observed before it."""
    suite.add("C6", c6_no_http_500, scored=False)
