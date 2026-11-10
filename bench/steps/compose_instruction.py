#!/usr/bin/env python3
"""Build and check step instructions.

``steps/step-N/instruction.md`` = preamble + ``environment/app/AGENTS.md`` (verbatim) +
separator + ``steps/step-N/task.md``. Every harness then sees the same conventions.

Usage (from anywhere):
    python bench/steps/compose_instruction.py            # build all steps that have task.md
    python bench/steps/compose_instruction.py --check    # verify, write nothing; exit 1 on drift

Steps without ``task.md`` still hold a placeholder instruction and are skipped;
``--check --strict`` treats them as failures.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

STEPS_DIR = Path(__file__).resolve().parent
AGENTS_MD = STEPS_DIR.parent / "environment" / "app" / "AGENTS.md"
PREAMBLE = (
    b"You work autonomously. There is no human to answer questions: do not ask for"
    b" confirmation or\nclarification \xe2\x80\x94 make reasonable decisions yourself and"
    b" finish the task.\n\n"
)
SEPARATOR = b"\n---\n\n"


def compose(agents: bytes, task: bytes) -> bytes:
    """Return the instruction bytes for one step."""
    return PREAMBLE + agents + SEPARATOR + task


def check_step(step_dir: Path, agents: bytes) -> str | None:
    """Return a problem description for the step, or None if its instruction is consistent."""
    instruction = step_dir / "instruction.md"
    if not instruction.is_file():
        return "instruction.md is missing"
    actual = instruction.read_bytes()
    if not actual.startswith(PREAMBLE):
        return "preamble is missing or differs"
    if not actual.startswith(PREAMBLE + agents + SEPARATOR):
        return "AGENTS block is not byte-identical to AGENTS.md"
    if actual != compose(agents, (step_dir / "task.md").read_bytes()):
        return "instruction.md is stale (differs from AGENTS.md + task.md)"
    return None


def main() -> int:
    """Build or check the instructions of all steps; return the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify only, write nothing")
    parser.add_argument("--strict", action="store_true", help="fail on steps without task.md")
    args = parser.parse_args()

    agents = AGENTS_MD.read_bytes()
    failed = False
    for step_dir in sorted(STEPS_DIR.glob("step-*")):
        if not (step_dir / "task.md").is_file():
            print(f"{step_dir.name}: no task.md, skipped")
            failed |= args.strict
            continue
        if args.check:
            problem = check_step(step_dir, agents)
            print(f"{step_dir.name}: {problem or 'ok'}")
            failed |= problem is not None
        else:
            task = (step_dir / "task.md").read_bytes()
            (step_dir / "instruction.md").write_bytes(compose(agents, task))
            print(f"{step_dir.name}: written")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
