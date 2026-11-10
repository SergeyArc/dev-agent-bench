"""Inventory of step-5 units for the volume estimate: C4 outcome and size of the agent's tree.

Reads only: `criteria.json` of the verifier (status, duration and test count of C4) and the
snapshot bundle (line counts of `src/tracksvc/` by layer and of `tests/`). Nothing is run.

    python3 bench-mut/inventory.py ~/bench-runs > inventory.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bench" / "report"))

from snapshots import Snapshot, SnapshotError  # noqa: E402

STEP = 5
PACKAGE = "src/tracksvc/"


def layer_of(path: str) -> str:
    """First component under src/tracksvc/: a package name or a module name without .py."""
    head = path[len(PACKAGE) :].split("/", 1)[0]
    return head.removesuffix(".py")


def c4(step_dir: Path) -> dict[str, object]:
    path = step_dir / "verifier" / "criteria.json"
    if not path.is_file():
        return {"status": "no criteria.json"}
    found: dict[str, object] = {"status": "absent"}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("id") == "C4":
                tests = re.search(
                    r"(\d+) test\(s\) passed", str(node.get("evidence", ""))
                )
                found.update(
                    status=node.get("status"),
                    seconds=node.get("duration_sec"),
                    tests=int(tests.group(1)) if tests else None,
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(path.read_text()))
    return found


def sizes(step_dir: Path) -> dict[str, object]:
    try:
        with Snapshot(step_dir, STEP) as snapshot:
            src: Counter[str] = Counter()
            tests = 0
            for path in snapshot.files():
                if not path.endswith(".py"):
                    continue
                lines = snapshot.read(path).count("\n")
                if path.startswith(PACKAGE):
                    src[layer_of(path)] += lines
                elif path.startswith("tests/"):
                    tests += lines
            return {"snapshot": "ok", "src_lines": dict(src), "test_lines": tests}
    except SnapshotError as error:
        return {"snapshot": str(error)}


def main() -> None:
    runs = Path(sys.argv[1]).expanduser()
    units = []
    for line in (HERE / "trials.tsv").read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        pair, variant, limit, trial, place = line.split("\t")
        step_dir = runs / trial / "steps" / f"step-{STEP}"
        unit: dict[str, object] = {
            "pair": pair, "variant": variant, "turn_limit": int(limit), "trial": trial, "place": place,
        }  # fmt: skip
        if not step_dir.is_dir():
            unit["c4"] = {"status": "no step 5"}
            unit["snapshot"] = "no step 5"
        else:
            unit["c4"] = c4(step_dir)
            unit.update(sizes(step_dir))
        units.append(unit)
    json.dump(units, sys.stdout, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
