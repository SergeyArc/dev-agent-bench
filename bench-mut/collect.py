"""Summarise one mutation session (the output directory of run_unit.sh) by layer.

    python3 bench-mut/collect.py <run-dir> [--json summary.json]

A layer is a shard: one entry of `src/tracksvc/` (package or module). Statuses follow
mutmut's own mapping of worker exit codes (mutmut/stats.py, 3.8.0):

    killed      a test failed (1, 3), or pytest could not collect because the mutant breaks
                the import of the application (255; mutmut itself calls this "timeout")
    survived    the covering tests passed (0)
    timeout     the run exceeded the limit derived from the tests' durations (36, ±24, 152)
    no tests    no test calls the mutated function in the test process (33, 5)
    other       suspicious / segfault / skipped / not checked — listed, never scored as killed

Two scores are reported. `score` counts mutants without a covering test as not killed:
(killed + timeout) / all mutants. `score_covered` is mutmut's own figure:
(killed + timeout) / (killed + timeout + survived).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
from collections import Counter
from pathlib import Path

PACKAGE = "src/tracksvc/"
KILLED = {1, 3, 255}
SURVIVED = {0}
TIMEOUT = {36, 24, -24, 152}
NO_TESTS = {33, 5}
COLUMNS = ("killed", "survived", "timeout", "no tests", "other")


def status_of(exit_code: int | None) -> str:
    if exit_code in KILLED:
        return "killed"
    if exit_code in SURVIVED:
        return "survived"
    if exit_code in TIMEOUT:
        return "timeout"
    if exit_code in NO_TESTS:
        return "no tests"
    return "other"


def layer_of(path: str) -> str:
    return path[len(PACKAGE) :].split("/", 1)[0].removesuffix(".py")


def read_json(path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def mutation_rate(log: Path) -> float | None:
    """Mutants per second as printed by mutmut at the end of the run."""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return None
    found = re.findall(r"([0-9.]+) mutations/second", text)
    return float(found[-1]) if found else None


def exit_codes(archive: Path, shard: str) -> dict[str, int | None]:
    """Exit code of every mutant of the layer, from the meta files of one container."""
    codes: dict[str, int | None] = {}
    if not archive.is_file():
        return codes
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            name = member.name.removeprefix("./")
            if (
                not name.endswith(".py.meta")
                or not name.startswith(PACKAGE)
                or layer_of(name[:-5]) != shard
            ):
                continue
            handle = tar.extractfile(member)
            if handle is not None:
                codes.update(json.load(handle).get("exit_code_by_key", {}))
    return codes


def shard_summary(shard_dir: Path, shard: str) -> dict[str, object]:
    codes = exit_codes(shard_dir / "mutants-meta.tar", shard)
    timeouts_first = sum(1 for code in codes.values() if status_of(code) == "timeout")
    # Mutants that timed out on an overloaded host were run again (batch.py): the last
    # verdict replaces the first.
    reruns = []
    for rerun in sorted(
        shard_dir.parent.glob(f"_rerun/{shard}-*"),
        key=lambda path: int(path.name.rsplit("-", 1)[1]),
    ):
        names_file = rerun / "rerun-mutants.txt"
        if not names_file.is_file():
            continue
        names = set(names_file.read_text().split())
        again = exit_codes(rerun / "mutants-meta.tar", shard)
        replaced = {name: again[name] for name in names if again.get(name) is not None}
        codes.update(replaced)
        reruns.append(
            {
                "dir": rerun.name,
                "mutants": len(names),
                "verdicts": len(replaced),
                **read_json(rerun / "load.json"),
            }
        )

    counts: Counter[str] = Counter()
    other: dict[str, int | None] = {}
    not_killed: dict[str, list[str]] = {"survived": [], "no tests": []}
    for mutant, exit_code in codes.items():
        status = status_of(exit_code)
        counts[status] += 1
        if status == "other":
            other[mutant] = exit_code
        elif status in not_killed:
            not_killed[status].append(mutant)
    total = sum(counts.values())
    caught = counts["killed"] + counts["timeout"]
    covered = caught + counts["survived"]
    run = read_json(shard_dir / "mutmut-run.json")
    rate = mutation_rate(shard_dir / "mutmut-run.log")
    seconds = (
        float(run["seconds"]) if isinstance(run.get("seconds"), (int, float)) else None
    )
    invocations = (
        shard_dir / "mutmut-invocations.tsv"
    )  # several when the session was stopped
    if invocations.is_file():
        seconds = sum(
            float(line.split("\t")[1])
            for line in invocations.read_text().splitlines()
            if line
        )
    crashes = sum(1 for code in codes.values() if code == 255)
    return {
        "layer": shard,
        "mutants": total,
        **{column: counts[column] for column in COLUMNS},
        "score": caught / total if total else None,
        "score_covered": caught / covered if covered else None,
        "mutmut_exit_code": run.get("exit_code"),
        "mutmut_seconds": seconds,
        # stats collection and the clean run are part of mutmut_seconds; the rate is not
        "seconds_per_mutant": 1 / rate if rate else None,
        "precondition": read_json(shard_dir / "precondition.json"),
        "postcheck": read_json(shard_dir / "postcheck.json"),
        "timeouts_first_run": timeouts_first,
        "collection_crashes": crashes,
        "reruns": reruns,
        "load": read_json(shard_dir / "load.json"),
        "other_mutants": other,
        "not_killed": not_killed,
    }


def total_row(layers: list[dict[str, object]]) -> dict[str, object]:
    row: dict[str, object] = {"layer": "all"}
    for key in ("mutants", *COLUMNS):
        row[key] = sum(int(layer[key]) for layer in layers)  # type: ignore[call-overload]
    caught = int(row["killed"]) + int(row["timeout"])  # type: ignore[call-overload]
    covered = caught + int(row["survived"])  # type: ignore[call-overload]
    total = int(row["mutants"])  # type: ignore[call-overload]
    row["score"] = caught / total if total else None
    row["score_covered"] = caught / covered if covered else None
    seconds = [layer["mutmut_seconds"] for layer in layers]
    row["mutmut_seconds"] = sum(s for s in seconds if isinstance(s, float))
    row["seconds_per_mutant"] = None
    return row


def fmt(value: object, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    # A package and a module of the same name (worker/, worker.py) are one layer.
    shards = list(dict.fromkeys((args.run_dir / "shards").read_text().split()))
    layers = [shard_summary(args.run_dir / shard, shard) for shard in shards]
    total = total_row(layers)
    wall = args.run_dir / "wall-seconds"
    summary = {
        "run_dir": str(args.run_dir),
        "wall_seconds": int(wall.read_text()) if wall.is_file() else None,
        "layers": layers,
        "total": total,
    }
    if args.json:
        args.json.write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n")

    header = (
        "layer",
        "mutants",
        *COLUMNS,
        "score",
        "score_covered",
        "mutmut_seconds",
        "seconds_per_mutant",
    )
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for row in [*layers, total]:
        digits = {"mutmut_seconds": 0, "seconds_per_mutant": 1}
        print(
            "| "
            + " | ".join(fmt(row.get(key), digits.get(key, 2)) for key in header)
            + " |"
        )
    print(f"\nwall: {summary['wall_seconds']} s")
    for layer in layers:
        pre, post = layer["precondition"], layer["postcheck"]
        assert isinstance(pre, dict) and isinstance(post, dict)
        if (
            pre.get("exit_code") != 0
            or post.get("exit_code") != 0
            or layer["mutmut_exit_code"] != 0
        ):
            print(
                f"ATTENTION {layer['layer']}: precondition={pre.get('exit_code')} "
                f"mutmut={layer['mutmut_exit_code']} postcheck={post.get('exit_code')}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
