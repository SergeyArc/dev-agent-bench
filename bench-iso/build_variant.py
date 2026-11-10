#!/usr/bin/env python3
"""Build the isolated variant of the task: one step N that starts from the reference state of step N-1.

    python3 bench-iso/build_variant.py --step 3                        # -> bench-iso/build/iso-s3/bench
    python3 bench-iso/build_variant.py --step 3 --task-md FILE         # ISO-SPEC: task.md of the step replaced
    python3 bench-iso/build_variant.py --step 3 --agents-md FILE       # ISO-AGENTS: /app/AGENTS.md replaced
    python3 bench-iso/build_variant.py --step 3 --out DIR
    python3 bench-iso/build_variant.py --hash                          # print the hash of the G0 task and exit

The variant is a copy of ``bench/`` (without ``run/`` and ``report/``) reduced to the
step N. ``bench/`` itself is only read: G0 stays byte-identical. The build is
deterministic - the same ``bench/`` and input files give the same bytes.

What differs from G0:
  environment/app/           + the reference tree after step N-1: the solutions of the steps
                               1..N-1 applied one after another, as their solve.sh does (files
                               of src/, tests/, migrations/ that the next tree does not have are
                               removed). The Dockerfile commits it as the initial commit of /app.
                               N = 1: nothing is added.
  environment/app/AGENTS.md  --agents-md: replaced by the given file
  steps/                     only step-N is kept (task.md, instruction.md, solution, tests, negatives)
  steps/step-N/task.md       --task-md: replaced by the given file
  steps/step-N/instruction.md  recomposed by bench/steps/compose_instruction.py; without
                               --task-md and --agents-md it equals the one of G0
  task.toml                  task name; only the [[steps]] block of step-N (BENCH_STEP = N)
  variant.json               what was built and from what

``tests/`` (verifier, hidden tests), the Dockerfile and everything else are those of G0.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Importing the composer of bench/ must not leave a __pycache__ there.
sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BENCH = REPO / "bench"
BUILD = HERE / "build"
TASK_NAME = "local/dev-agent-bench"
STEPS = range(1, 6)
# Not part of the task: the runner and the report tools.
NOT_TASK = ("run", "report")
IGNORED = (*NOT_TASK, "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store")
# The directories a solve.sh prunes before it copies its tree.
PRUNED_DIRS = ("src", "tests", "migrations")
# The tool configuration is frozen: a solution must never carry it (as in solve.sh).
FROZEN = ("ruff.toml", "mypy.ini")


def fail(message: str) -> SystemExit:
    return SystemExit(f"build_variant: {message}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_tree(root: Path, ignored: tuple[str, ...] = ()) -> str:
    """Hash of file paths, modes and contents under ``root`` (stable order)."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root)
        if any(part in ignored for part in relative.parts):
            continue
        executable = b"x" if path.stat().st_mode & 0o111 else b"-"
        digest.update(relative.as_posix().encode() + b"\0" + executable + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def load_composer():  # type: ignore[no-untyped-def]
    """bench/steps/compose_instruction.py as a module: the composer of G0, unchanged."""
    spec = importlib.util.spec_from_file_location("compose_instruction", BENCH / "steps" / "compose_instruction.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def variant_name(task_md: bool, agents_md: bool) -> str:
    return "ISO" + ("-SPEC" if task_md else "") + ("-AGENTS" if agents_md else "")


def default_out(step: int, variant: str) -> Path:
    return BUILD / f"{variant.lower()}-s{step}" / "bench"


def solution_files(step: int) -> dict[str, Path]:
    """Files of the reference tree of a step, by path relative to the project root."""
    root = BENCH / "steps" / f"step-{step}" / "solution" / "app"
    if not root.is_dir():
        raise fail(f"{root.relative_to(REPO)} is missing")
    files = {
        path.relative_to(root).as_posix(): path
        for path in sorted(root.rglob("*"))
        if path.is_file() and not any(part in IGNORED for part in path.relative_to(root).parts)
    }
    for name in FROZEN:
        if name in files:
            raise fail(f"step-{step}: {name} must not be part of the solution")
    return files


def apply_solution(app: Path, step: int) -> None:
    """What steps/step-N/solution/solve.sh does to the project root."""
    files = solution_files(step)
    for name in PRUNED_DIRS:
        directory = app / name
        if not directory.is_dir():
            continue
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            if path.relative_to(app).as_posix() not in files:
                path.unlink()
        for path in sorted((p for p in directory.rglob("*") if p.is_dir()), reverse=True):
            if not any(path.iterdir()):
                path.rmdir()
        if not any(directory.iterdir()):
            directory.rmdir()
    for relative, source in files.items():
        target = app / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)


def keep_step(toml: str, step: int) -> str:
    """task.toml with the variant's task name and the [[steps]] block of one step only."""
    old = f'name = "{TASK_NAME}"'
    if toml.count(old) != 1:
        raise fail(f"expected exactly one task name in task.toml, found {toml.count(old)}")
    toml = toml.replace(old, f'name = "{TASK_NAME}-iso-s{step}"')
    # The same split as prepare_task of bench/run/run_trial.py.
    head, *blocks = re.split(r"(?m)^(?=\[\[steps\]\]\s*$)", toml)
    kept = [b for b in blocks if re.match(rf'\[\[steps\]\]\s*\nname\s*=\s*"step-{step}"', b)]
    if len(kept) != 1 or f'BENCH_STEP = "{step}"' not in kept[0]:
        raise fail(f"expected one [[steps]] block of step-{step} with BENCH_STEP = \"{step}\" in task.toml")
    return head + kept[0]


def build(step: int, out: Path, task_md: Path | None = None, agents_md: Path | None = None) -> dict[str, object]:
    if step not in STEPS:
        raise fail(f"--step must be one of {list(STEPS)}")
    variant = variant_name(task_md is not None, agents_md is not None)
    # Read before anything is removed: an input may live inside a previous build.
    new_task = task_md.read_bytes() if task_md else None
    new_agents = agents_md.read_bytes() if agents_md else None
    if out.exists():
        if not (out / "variant.json").is_file():
            raise fail(f"{out} exists and is not a built variant; refusing to replace it")
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(BENCH, out, ignore=shutil.ignore_patterns(*IGNORED))

    # --- the step ------------------------------------------------------------
    name = f"step-{step}"
    for other in sorted((out / "steps").glob("step-*")):
        if other.name != name:
            shutil.rmtree(other)
    toml_path = out / "task.toml"
    toml_path.write_text(keep_step(toml_path.read_text(), step))

    # --- initial commit of /app: the reference state of step N-1 --------------
    app = out / "environment" / "app"
    for previous in range(1, step):
        apply_solution(app, previous)
    if new_agents is not None:
        (app / "AGENTS.md").write_bytes(new_agents)
    agents = (app / "AGENTS.md").read_bytes()

    # --- instruction ---------------------------------------------------------
    composer = load_composer()
    step_dir = out / "steps" / name
    task = step_dir / "task.md"
    if new_task is not None:
        task.write_bytes(new_task)
    instruction = composer.compose(agents, task.read_bytes())
    if new_task is None and new_agents is None and instruction != (step_dir / "instruction.md").read_bytes():
        raise fail(f"{name}: instruction.md of bench/ is stale (run its composer with --check)")
    (step_dir / "instruction.md").write_bytes(instruction)
    problem = composer.check_step(step_dir, agents)
    if problem:
        raise fail(f"{name}: {problem}")

    git = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    info: dict[str, object] = {
        "variant": variant,
        "step": step,
        "task_name": f"{TASK_NAME}-iso-s{step}",
        "steps": [name],
        # The initial commit of /app: the reference tree after this step (0 = the empty project of G0).
        "start_state": {"reference_step": step - 1, "app_sha256": sha256_tree(app)},
        "task_md_sha256": sha256_bytes(task.read_bytes()),
        "task_md_replaced": new_task is not None,
        "agents_md_sha256": sha256_bytes(agents),
        "agents_md_replaced": new_agents is not None,
        "instruction_sha256": sha256_bytes(instruction),
        "source_commit": git.stdout.strip() or None,
        "source_bench_sha256": sha256_tree(BENCH, IGNORED),
        "builder_sha256": sha256_bytes(Path(__file__).read_bytes()),
    }
    (out / "variant.json").write_text(json.dumps(info, indent=2) + "\n")
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--step", type=int, choices=list(STEPS), help="the step N of the task; it starts from the reference state of step N-1")
    parser.add_argument("--task-md", type=Path, help="file that replaces task.md of the step (variant ISO-SPEC)")
    parser.add_argument("--agents-md", type=Path, help="file that replaces /app/AGENTS.md of the initial commit (variant ISO-AGENTS)")
    parser.add_argument("--out", type=Path, help="task directory to write (default bench-iso/build/<variant>-s<N>/bench)")
    parser.add_argument("--hash", action="store_true", help="print the hash of bench/ (without run/ and report/) and exit")
    args = parser.parse_args()
    if args.hash:
        print(sha256_tree(BENCH, IGNORED))
        return 0
    if args.step is None:
        parser.error("--step is required")
    for path in (args.task_md, args.agents_md):
        if path is not None and not path.is_file():
            parser.error(f"{path} is not a file")
    before = sha256_tree(BENCH, IGNORED)
    out = args.out or default_out(args.step, variant_name(args.task_md is not None, args.agents_md is not None))
    info = build(args.step, out.expanduser().resolve(), args.task_md, args.agents_md)
    if sha256_tree(BENCH, IGNORED) != before:
        raise fail("bench/ changed during the build")
    print(json.dumps({"out": str(out), **info}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
