#!/usr/bin/env python3
"""Self-test of the isolated variant without Docker: the build for every step, the
initial tree against the cascade of the real solve.sh scripts, what differs from
bench/, and what the runner records. Run: python3 bench-iso/tests/test_iso.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ISO_DIR = HERE.parent
REPO = ISO_DIR.parent
BENCH = REPO / "bench"
sys.path.insert(0, str(BENCH / "run"))
sys.path.insert(0, str(ISO_DIR))

import build_variant  # noqa: E402
import run_trial  # noqa: E402
import runjson  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: Any = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail != "" and not ok else ""))
    if not ok:
        failures.append(name)


def files(root: Path, ignored: tuple[str, ...] = build_variant.IGNORED) -> dict[str, tuple[bytes, bool]]:
    """Path -> (content, executable) of every file under ``root``."""
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), bool(path.stat().st_mode & 0o111))
        for path in sorted(root.rglob("*"))
        if path.is_file() and not any(part in ignored for part in path.relative_to(root).parts)
    }


def differing(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    return sorted(key for key in left.keys() | right.keys() if left.get(key) != right.get(key))


def git_tree(directory: Path, scratch: Path) -> str:
    """Tree hash of what `git add -A` of the Dockerfile would commit (honours .gitignore)."""
    env = {**os.environ, "GIT_DIR": str(scratch / f"git-{directory.parent.name}-{directory.name}"), "GIT_WORK_TREE": str(directory)}
    subprocess.run(["git", "init", "-q"], env=env, check=True)
    subprocess.run(["git", "add", "-A"], env=env, check=True)
    return subprocess.run(["git", "write-tree"], env=env, check=True, capture_output=True, text=True).stdout.strip()


def cascade(step: int, dest: Path) -> None:
    """The project root after the oracle of the steps 1..N-1: G0's app/ and the real solve.sh scripts."""
    shutil.copytree(BENCH / "environment" / "app", dest)
    for previous in range(1, step):
        script = BENCH / "steps" / f"step-{previous}" / "solution" / "solve.sh"
        subprocess.run(["bash", str(script)], env={**os.environ, "APP_DIR": str(dest)}, check=True, capture_output=True)


def test_step(step: int, tmp: Path) -> Path:
    label = f"s{step}"
    name = f"step-{step}"
    out = tmp / f"iso-{label}" / "bench"
    info = build_variant.build(step, out)
    bench, built = files(BENCH), files(out)

    # --- the initial tree ------------------------------------------------------
    reference = tmp / f"cascade-{label}" / "app"
    reference.parent.mkdir()
    cascade(step, reference)
    app = out / "environment" / "app"
    check(f"{label}: initial tree equals the cascade of solve.sh 1..{step - 1}, byte for byte",
          files(app) == files(reference), differing(files(app), files(reference)))
    check(f"{label}: git tree of the initial commit equals the one of the cascade", git_tree(app, tmp) == git_tree(reference, tmp))
    g0_app = files(BENCH / "environment" / "app")
    if step == 1:
        check(f"{label}: initial tree is the one of G0", files(app) == g0_app)
    else:
        solution = files(BENCH / "steps" / f"step-{step - 1}" / "solution" / "app")
        check(f"{label}: initial tree = G0 app/ + the reference tree of step {step - 1}, nothing else",
              files(app) == {**g0_app, **solution} and not (g0_app.keys() & solution.keys()))
        for directory in build_variant.PRUNED_DIRS:
            check(f"{label}: {directory}/ equals the reference of step {step - 1}",
                  files(app / directory) == files(BENCH / "steps" / f"step-{step - 1}" / "solution" / "app" / directory))
    check(f"{label}: frozen configuration and lock are those of G0",
          all(files(app)[f] == g0_app[f] for f in ("ruff.toml", "mypy.ini", "pyproject.toml", "uv.lock", "AGENTS.md", ".gitignore")))

    # --- everything else against bench/ ----------------------------------------
    other_steps = {k for k in bench if k.startswith("steps/step-") and not k.startswith(f"steps/{name}/")}
    added = {k for k in built if k not in bench}
    expected_added = {"variant.json"} | {f"environment/app/{k}" for k in files(app) if k not in g0_app}
    check(f"{label}: added files are variant.json and the initial tree only", added == expected_added, sorted(added ^ expected_added))
    check(f"{label}: removed files are the other steps only", {k for k in bench if k not in built} == other_steps)
    changed = sorted(k for k in built if k in bench and built[k] != bench[k])
    check(f"{label}: the only changed file is task.toml", changed == ["task.toml"], changed)
    for part in ("tests/", f"steps/{name}/tests/", f"steps/{name}/solution/", "environment/verifier/"):
        same = {k: v for k, v in built.items() if k.startswith(part)} == {k: v for k, v in bench.items() if k.startswith(part)}
        check(f"{label}: {part} is byte-identical to bench/", same and any(k.startswith(part) for k in built))
    for part in ("task.md", "instruction.md"):
        check(f"{label}: {part} of the step equals bench/", built[f"steps/{name}/{part}"] == bench[f"steps/{name}/{part}"])
    check(f"{label}: Dockerfile is the one of G0", built["environment/Dockerfile"] == bench["environment/Dockerfile"])
    check(f"{label}: no run/ and report/ in the build", not any(k.split("/")[0] in build_variant.NOT_TASK for k in built))

    # --- task.toml -------------------------------------------------------------
    toml = tomllib.loads((out / "task.toml").read_text())
    g0_toml = tomllib.loads((BENCH / "task.toml").read_text())
    check(f"{label}: task name", toml["task"]["name"] == f"local/dev-agent-bench-iso-s{step}")
    check(f"{label}: one step, the block of {name} of G0", toml["steps"] == [g0_toml["steps"][step - 1]] and toml["steps"][0]["name"] == name)
    check(f"{label}: BENCH_STEP and the snapshot label are those of the step",
          toml["steps"][0]["verifier"]["env"] == {"BENCH_STEP": str(step)}
          and toml["steps"][0]["verifier"]["collect"][0]["command"] == f"/usr/local/bin/bench-snapshot {name}")
    rest = {k: v for k, v in toml.items() if k != "steps"}
    rest["task"] = {**rest["task"], "name": g0_toml["task"]["name"]}
    check(f"{label}: the rest of task.toml is the one of G0", rest == {k: v for k, v in g0_toml.items() if k != "steps"})

    # --- composer, variant.json, runner ---------------------------------------
    composer = subprocess.run([sys.executable, "-B", str(out / "steps" / "compose_instruction.py"), "--check", "--strict"],
                              capture_output=True, text=True, check=False)
    check(f"{label}: composer --check of the build", composer.returncode == 0 and composer.stdout.strip() == f"{name}: ok", composer.stdout)
    check(f"{label}: variant.json", (info["variant"], info["step"], info["steps"], info["start_state"]["reference_step"]) == ("ISO", step, [name], step - 1)  # type: ignore[index]
          and info["task_md_sha256"] == build_variant.sha256_bytes(bench[f"steps/{name}/task.md"][0])
          and info["agents_md_sha256"] == build_variant.sha256_bytes(g0_app["AGENTS.md"][0])
          and json.loads((out / "variant.json").read_text()) == info and bool(info["source_commit"]))
    trial = tmp / f"trial-{label}"
    task_dir, names = run_trial.prepare_task(trial, 1, out)
    check(f"{label}: runner --steps 1 keeps the one step {name}", names == [name] and files(task_dir) == built)
    check(f"{label}: runner reads the variant from the task copy", runjson._load(task_dir / "variant.json")["variant"] == "ISO")
    hashes = runjson.input_hashes(REPO, names, out)
    check(f"{label}: run.json hashes describe the build",
          hashes["agents_md"] == info["agents_md_sha256"] and hashes["step_instructions"] == {name: info["instruction_sha256"]}
          and hashes["task_toml"] == build_variant.sha256_bytes((out / "task.toml").read_bytes()))
    if step > 1:
        check(f"{label}: environment hash differs from G0", hashes["environment_context"] != runjson.input_hashes(REPO, names)["environment_context"])
    return out


def test_replacements(step: int, iso: Path, tmp: Path) -> None:
    label = f"s{step}"
    name = f"step-{step}"
    base = files(iso)
    task_md = tmp / "edited-task.md"
    task_md.write_bytes(base[f"steps/{name}/task.md"][0] + b"\n## Clarification\n\nSelf-test edit of the task.\n")
    agents_md = tmp / "edited-AGENTS.md"
    agents_md.write_bytes(base["environment/app/AGENTS.md"][0] + b"\n## Self-test\n\nSelf-test edit of the conventions.\n")
    cases = [
        ("ISO-SPEC", task_md, None, {f"steps/{name}/task.md"}),
        ("ISO-AGENTS", None, agents_md, {"environment/app/AGENTS.md"}),
        ("ISO-SPEC-AGENTS", task_md, agents_md, {f"steps/{name}/task.md", "environment/app/AGENTS.md"}),
    ]
    for variant, task, agents, replaced in cases:
        tag = f"{label} {variant}"
        out = tmp / f"{variant.lower()}-{label}" / "bench"
        info = build_variant.build(step, out, task, agents)
        built = files(out)
        expected = sorted(replaced | {f"steps/{name}/instruction.md", "variant.json"})
        check(f"{tag}: differs from ISO in {len(expected)} files only", differing(base, built) == expected, differing(base, built))
        check(f"{tag}: variant name", info["variant"] == variant and build_variant.default_out(step, variant).parent.name == f"{variant.lower()}-{label}")
        check(f"{tag}: task.md is {'the given file' if task else 'the one of bench/'}",
              built[f"steps/{name}/task.md"][0] == (task.read_bytes() if task else base[f"steps/{name}/task.md"][0])
              and info["task_md_sha256"] == build_variant.sha256_bytes(built[f"steps/{name}/task.md"][0]) and info["task_md_replaced"] == bool(task))
        agents_bytes = built["environment/app/AGENTS.md"][0]
        check(f"{tag}: AGENTS.md is {'the given file' if agents else 'the one of bench/'}",
              agents_bytes == (agents.read_bytes() if agents else base["environment/app/AGENTS.md"][0])
              and info["agents_md_sha256"] == build_variant.sha256_bytes(agents_bytes) and info["agents_md_replaced"] == bool(agents))
        instruction = built[f"steps/{name}/instruction.md"][0]
        check(f"{tag}: instruction = preamble + AGENTS.md + task.md of the build, each once",
              instruction.count(agents_bytes) == 1 and instruction.endswith(built[f"steps/{name}/task.md"][0])
              and instruction.count(b"Self-test edit of the task.") == int(bool(task))
              and instruction.count(b"Self-test edit of the conventions.") == int(bool(agents)))
        composer = subprocess.run([sys.executable, "-B", str(out / "steps" / "compose_instruction.py"), "--check", "--strict"],
                                  capture_output=True, text=True, check=False)
        check(f"{tag}: composer --check of the build", composer.returncode == 0 and composer.stdout.strip() == f"{name}: ok", composer.stdout)
        _, names = run_trial.prepare_task(tmp / f"trial-{variant.lower()}-{label}", 1, out)
        hashes = runjson.input_hashes(REPO, names, out)
        check(f"{tag}: AGENTS.md hash of run.json is the one of the file in the initial commit",
              hashes["agents_md"] == info["agents_md_sha256"] == build_variant.sha256_bytes(agents_bytes)
              and hashes["step_instructions"][name] == build_variant.sha256_bytes(instruction))


def main() -> int:
    before = build_variant.sha256_tree(BENCH, build_variant.IGNORED)
    whole_before = build_variant.sha256_tree(BENCH)
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory).resolve()
        built = {step: test_step(step, tmp) for step in build_variant.STEPS}
        for step in (1, 3, 5):
            test_replacements(step, built[step], tmp)

        first = build_variant.sha256_tree(built[4])
        again = tmp / "again" / "bench"
        build_variant.build(4, again)
        check("the build is deterministic", build_variant.sha256_tree(again) == first)
        build_variant.build(4, again)
        check("a built variant is replaced by the next build", build_variant.sha256_tree(again) == first)
        foreign = tmp / "foreign"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("not a variant\n")
        try:
            build_variant.build(2, foreign)
            refused = False
        except SystemExit:
            refused = True
        check("a directory that is not a built variant is not replaced", refused and (foreign / "keep.txt").is_file())
        cli = subprocess.run([sys.executable, "-B", str(ISO_DIR / "build_variant.py"), "--step", "2", "--out", str(tmp / "cli" / "bench")],
                             capture_output=True, text=True, check=False)
        check("command line build", cli.returncode == 0 and json.loads(cli.stdout)["variant"] == "ISO"
              and build_variant.sha256_tree(tmp / "cli" / "bench") == build_variant.sha256_tree(built[2]), cli.stderr)
    check("bench/ did not change", build_variant.sha256_tree(BENCH, build_variant.IGNORED) == before and build_variant.sha256_tree(BENCH) == whole_before)
    check("no __pycache__ left in bench/steps and bench-iso", not list((BENCH / "steps").rglob("__pycache__")) and not list(ISO_DIR.rglob("__pycache__")))
    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
