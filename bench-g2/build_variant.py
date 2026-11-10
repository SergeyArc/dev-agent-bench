#!/usr/bin/env python3
"""Build the G2 variant of the task (ablation: pre-commit hook that runs `make check`).

    python3 bench-g2/build_variant.py                  # -> bench-g2/build/bench
    python3 bench-g2/build_variant.py --variant g2b    # -> bench-g2/build/g2b/bench
    python3 bench-g2/build_variant.py --out DIR
    python3 bench-g2/build_variant.py --selftest       # step 1 of the oracle also exercises the hook
    python3 bench-g2/build_variant.py --hash           # print the hash of the G0 task and exit

The variant is a copy of ``bench/`` (without ``run/``) plus the overlay of this
directory. ``bench/`` itself is only read: G0 stays byte-identical. The build is
deterministic - the same ``bench/`` and overlay give the same bytes.

What differs from G0:
  environment/app/           + Makefile, .importlinter, .githooks/pre-commit (in the initial commit)
  environment/g2/            + bench-g2-collect (collect hook, root)
  environment/Dockerfile     + one block: collect hook, `lint-imports` on PATH, hook log
                               directory, baseline hashes, `core.hooksPath` of the repository
  task.toml                  task name; a second collect hook per step, after bench-snapshot
  steps/*/task.md            + the section of task-section.md at the end
  steps/*/instruction.md     recomposed by bench/steps/compose_instruction.py from the same
                               AGENTS.md and the extended task.md
  steps/*/solution/solve.sh  + the oracle commits the step (through the hook)
  variant.json               what was built and from what

G2b is G2 with the same section in another place: it is appended to
``environment/app/AGENTS.md`` (the initial commit of /app) instead of every ``task.md``.
``task.md`` is then the one of G0, and the instruction carries the section once, inside
its AGENTS.md block. Nothing else differs from G2 but the task name and variant.json.
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
OVERLAY = HERE / "overlay"
SECTION = HERE / "task-section.md"
DEFAULT_OUT = HERE / "build" / "bench"
VARIANT = "g2"
TASK_NAME = ("local/dev-agent-bench", "local/dev-agent-bench-g2")
# variant -> (name in variant.json, task name, default output, file that gets the section)
VARIANTS = {
    "g2": (VARIANT, TASK_NAME[1], DEFAULT_OUT, "task.md"),
    "g2b": ("G2b", TASK_NAME[1] + "b", HERE / "build" / "g2b" / "bench", "AGENTS.md"),
}
IGNORED = ("run", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store")

DOCKERFILE_ANCHOR = "COPY entrypoint.sh /entrypoint.sh\n"
DOCKERFILE_BLOCK = """\
# --- G2 ablation: git pre-commit hook that runs `make check` -----------------
# Makefile, .importlinter and .githooks/ come with app/ and are part of the
# initial commit. import-linter is the verifier's build, put on PATH; the hook
# log lives outside /app; the hashes are the baseline of the bypass detectors.
COPY g2/bench-g2-collect /usr/local/bin/bench-g2-collect
RUN chmod 0755 /usr/local/bin/bench-g2-collect \\
 && ln -s /opt/verifier/bin/lint-imports /usr/local/bin/lint-imports \\
 && install -d -o agent -g agent /var/log/app-hooks \\
 && install -d /opt/verifier/baseline-g2 \\
 && test -x /app/.githooks/pre-commit \\
 && (cd /app && sha256sum Makefile .importlinter .githooks/pre-commit > /opt/verifier/baseline-g2/SHA256SUMS) \\
 && chmod -R a+rX,go-w /opt/verifier/baseline-g2
USER agent
# Set after the initial commit (an empty project cannot pass the checks). The
# hook path is repository configuration, so it works for the `agent` user.
RUN git -C /app config core.hooksPath .githooks \\
 && git -C /app config user.name dev \\
 && git -C /app config user.email dev@localhost \\
 && test -z "$(git -C /app status --porcelain)"
USER root

"""

COLLECT_BLOCK = re.compile(
    r'(\[\[steps\.verifier\.collect\]\]\ncommand = "/usr/local/bin/bench-snapshot (?P<step>[\w-]+)"\n'
    r'user = "root"\ntimeout_sec = [\d.]+\n)'
)
G2_COLLECT = (
    '[[steps.verifier.collect]]\ncommand = "/usr/local/bin/bench-g2-collect {step}"\n'
    'user = "root"\ntimeout_sec = 120.0\n'
)

ORACLE_COMMIT = """
# --- G2: commit the step; the pre-commit hook runs `make check` -------------
if [[ "$(id -u)" == 0 ]]; then
  echo "{step}: the G2 oracle commits as the agent user, not as root" >&2
  exit 1
fi
git -C "$DEST" add -A
git -C "$DEST" commit -q -m "{step}: reference solution"
echo "{step}: committed through the pre-commit hook"
"""

# Step 1 of the self-test build: the hook must reject a commit with a ruff
# error; a `--no-verify` commit that also edits the Makefile must show up in the
# detectors; the step still ends with the reference tree, committed and green.
ORACLE_SELFTEST = """
# --- G2 self-test: the hook blocks, the detectors see a bypass --------------
if [[ "$(id -u)" == 0 ]]; then
  echo "{step}: the G2 oracle commits as the agent user, not as root" >&2
  exit 1
fi
cd "$DEST"
start="$(git rev-parse HEAD)"
printf 'import os\\n' > src/tracksvc/g2_selftest.py
git add -A
if git commit -q -m "self-test: ruff error"; then
  echo "G2-SELFTEST: FAIL - a commit with a ruff error went through" >&2
  exit 1
fi
if [[ "$(git rev-parse HEAD)" != "$start" ]]; then
  echo "G2-SELFTEST: FAIL - HEAD moved although the hook rejected the commit" >&2
  exit 1
fi
echo "G2-SELFTEST: the hook rejected a commit with a ruff error"
echo "# self-test" >> Makefile
git add -A
git commit -q --no-verify -m "self-test: bypass"
git rm -q -f src/tracksvc/g2_selftest.py
git checkout -q "$start" -- Makefile
git add -A
git commit -q -m "{step}: reference solution"
echo "G2-SELFTEST: ok"
"""


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


def replace_once(text: str, old: str, new: str, what: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"build_variant: expected exactly one {what} in the G0 file, found {text.count(old)}")
    return text.replace(old, new)


def build(out: Path, selftest: bool, variant: str = VARIANT) -> dict[str, object]:
    variant_name, task_name, _, section_in = VARIANTS[variant]
    if out.exists():
        if not (out / "variant.json").is_file():
            raise SystemExit(f"build_variant: {out} exists and is not a built variant; refusing to replace it")
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(BENCH, out, ignore=shutil.ignore_patterns(*IGNORED))

    # --- environment ---------------------------------------------------------
    app = out / "environment" / "app"
    for source in sorted(p for p in (OVERLAY / "app").rglob("*") if p.is_file()):
        target = app / source.relative_to(OVERLAY / "app")
        if target.exists():
            raise SystemExit(f"build_variant: {target.relative_to(out)} already exists in G0")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if source.name == "pre-commit" else 0o644)
    image = out / "environment" / "g2"
    image.mkdir()
    shutil.copyfile(OVERLAY / "image" / "bench-g2-collect", image / "bench-g2-collect")
    (image / "bench-g2-collect").chmod(0o755)
    dockerfile = out / "environment" / "Dockerfile"
    dockerfile.write_text(
        replace_once(dockerfile.read_text(), DOCKERFILE_ANCHOR, DOCKERFILE_BLOCK + DOCKERFILE_ANCHOR, "entrypoint COPY")
    )

    # --- task.toml -----------------------------------------------------------
    toml_path = out / "task.toml"
    toml = replace_once(toml_path.read_text(), f'name = "{TASK_NAME[0]}"', f'name = "{task_name}"', "task name")
    steps = re.findall(r'^\[\[steps\]\]\s*\nname\s*=\s*"([^"]+)"', toml, flags=re.M)
    toml, replaced = COLLECT_BLOCK.subn(lambda m: m.group(1) + "\n" + G2_COLLECT.format(step=m.group("step")), toml)
    if replaced != len(steps) or not steps:
        raise SystemExit(f"build_variant: {len(steps)} steps but {replaced} bench-snapshot collect hooks in task.toml")
    toml_path.write_text(toml)

    # --- instructions and oracle ---------------------------------------------
    composer = load_composer()
    section = SECTION.read_bytes()
    if section_in == "AGENTS.md":
        (app / "AGENTS.md").write_bytes((app / "AGENTS.md").read_bytes() + section)
    agents = (app / "AGENTS.md").read_bytes()
    for name in steps:
        step_dir = out / "steps" / name
        task = step_dir / "task.md"
        if section_in == "task.md":
            task.write_bytes(task.read_bytes() + section)
        instruction = composer.compose(agents, task.read_bytes())
        if instruction.count(section) != 1:
            raise SystemExit(f"build_variant: {name}: the section is in the instruction {instruction.count(section)} times")
        (step_dir / "instruction.md").write_bytes(instruction)
        solve = step_dir / "solution" / "solve.sh"
        tail = ORACLE_SELFTEST if selftest and name == steps[0] else ORACLE_COMMIT
        solve.write_text(solve.read_text() + tail.format(step=name))

    commit = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    info: dict[str, object] = {
        "variant": variant_name,
        "selftest": selftest,
        "task_name": task_name,
        "steps": steps,
        "source_commit": commit or None,
        "source_bench_sha256": sha256_tree(BENCH, IGNORED),
        # Everything of this directory that shapes the result: the overlay, the section, this script.
        "overlay_sha256": sha256_tree(HERE, ("build", "tests", "README.md", ".gitignore", "agents_in_traffic.py", *IGNORED)),
    }
    (out / "variant.json").write_text(json.dumps(info, indent=2) + "\n")
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--variant", choices=sorted(VARIANTS), default=VARIANT, help="g2 (default) or g2b: the section in AGENTS.md")
    parser.add_argument("--out", type=Path, help=f"task directory to write (default {DEFAULT_OUT}, g2b: {VARIANTS['g2b'][2]})")
    parser.add_argument("--selftest", action="store_true", help="oracle of step 1 also exercises the hook and a bypass")
    parser.add_argument("--hash", action="store_true", help="print the hash of bench/ (without run/) and exit")
    args = parser.parse_args()
    if args.hash:
        print(sha256_tree(BENCH, IGNORED))
        return 0
    before = sha256_tree(BENCH, IGNORED)
    out = args.out or VARIANTS[args.variant][2]
    info = build(out.expanduser().resolve(), args.selftest, args.variant)
    if sha256_tree(BENCH, IGNORED) != before:
        raise SystemExit("build_variant: bench/ changed during the build")
    print(json.dumps({"out": str(out), **info}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
