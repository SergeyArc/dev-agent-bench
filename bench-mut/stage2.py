"""Tables of the full run (stage 2) from the results of batch.py; nothing is run.

    python3 bench-mut/stage2.py <out-root> [--json <file>]

Per unit and layer: the mutants and three scores —
  all        (killed + timeout) / mutants
  corrected  the same, plus the "no tests" mutants the whole suite kills (recheck)
  covered    (killed + timeout) / (killed + timeout + survived)
A timeout counts as caught; `corrected_low` counts it as survived.
Prints Markdown (the report is in Russian, so are the headings).
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

RECHECK_TIMEOUT = {124, 137}
STAGE1 = {
    "gemma-opencode-g0-5411de": "gemma-opencode-5411de"
}  # measured before the batch


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def recheck(directory: Path) -> tuple[dict[str, int], int, Counter[int]]:
    """Exit code per rechecked mutant, the number of failed controls, peak loads seen."""
    codes: dict[str, int] = {}
    bad_controls = 0
    files = sorted(directory.glob("*/recheck.tsv")) or sorted(
        directory.glob("recheck.tsv")
    )
    for tsv in sorted(files, key=lambda p: ("-r" in p.parent.name, p.parent.name)):
        control = (tsv.parent / "control.tsv").read_text().split()
        bad_controls += control[-1:] != ["0"]
        for line in tsv.read_text().splitlines():
            if line:
                mutant, code, _ = line.split("\t")
                codes[mutant] = int(code)
    return codes, bad_controls, Counter(codes.values())


def container_seconds(*roots: Path) -> int:
    total = 0
    for root in roots:
        for log in root.rglob("load.jsonl"):
            total += sum(
                json.loads(line)["seconds"] for line in log.read_text().splitlines()
            )
    return total


def unit_rows(out_root: Path, name: str, meta: dict) -> dict:
    run_name = STAGE1.get(name, name)
    run_dir = out_root / "runs" / run_name
    summary = read_json(run_dir / "summary.json")
    codes, bad_controls, code_counts = recheck(out_root / "recheck" / run_name)
    layers = []
    for layer in summary["layers"]:
        no_tests = layer["not_killed"]["no tests"]
        missing = [m for m in no_tests if m not in codes]
        extra = sum(1 for m in no_tests if codes.get(m, 0) not in (0, *RECHECK_TIMEOUT))
        slow = sum(1 for m in no_tests if codes.get(m) in RECHECK_TIMEOUT)
        peaks = [layer["load"].get("peak_load")] + [
            r.get("peak_load") for r in layer["reruns"]
        ]
        layers.append(
            {
                "layer": layer["layer"],
                **{k: layer[k] for k in ("mutants", "killed", "survived", "timeout")},
                "no_tests": len(no_tests),
                "recheck_killed": extra,
                "recheck_timeout": slow,
                "recheck_missing": len(missing),
                "timeouts_first_run": layer["timeouts_first_run"],
                "crashes": layer["collection_crashes"],
                "peak_first": peaks[0],
                "peak_last": [p for p in peaks if p is not None][-1:] or None,
                "reruns": len(layer["reruns"]),
                "tests": layer["precondition"].get("summary", "").strip("= "),
            }
        )
    total = {"layer": "all"}
    for key in (
        "mutants",
        "killed",
        "survived",
        "timeout",
        "no_tests",
        "recheck_killed",
        "recheck_timeout",
        "recheck_missing",
        "timeouts_first_run",
        "crashes",
    ):
        total[key] = sum(layer[key] for layer in layers)
    rows = [*layers, total]
    for row in rows:
        caught = row["killed"] + row["timeout"]
        covered = caught + row["survived"]
        n = row["mutants"]
        row["score"] = caught / n if n else None
        row["corrected"] = (caught + row["recheck_killed"]) / n if n else None
        row["corrected_low"] = (
            (row["killed"] + row["recheck_killed"]) / n if n else None
        )
        row["covered"] = caught / covered if covered else None
    peaks = [layer["peak_first"] for layer in layers if layer["peak_first"]]
    return {
        "unit": name,
        **meta,
        "layers": layers,
        "total": total,
        "bad_controls": bad_controls,
        "recheck_codes": dict(code_counts),
        "peak_min": min(peaks) if peaks else None,
        "peak_max": max(peaks) if peaks else None,
        "container_seconds": container_seconds(
            run_dir, out_root / "recheck" / run_name
        ),
    }


def fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}".replace(".", ",")
    return str(value)


def table(header: list[str], rows: list[list[object]]) -> None:
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for row in rows:
        print("| " + " | ".join(fmt(cell) for cell in row) + " |")
    print()


def spread(values: list[float]) -> str:
    if not values:
        return "—"
    return f"{fmt(statistics.median(values))} ({fmt(min(values))}–{fmt(max(values))})"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out_root", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    out_root = args.out_root.expanduser()

    inventory = json.loads((out_root / "inventory.json").read_text())
    units = []
    for entry in inventory:
        if entry.get("snapshot") != "ok" or entry["c4"].get("status") != "pass":
            continue
        if entry["place"].startswith("out"):
            continue
        pair = "".join(c if c.isalnum() else "-" for c in entry["pair"].lower())
        while "--" in pair:
            pair = pair.replace("--", "-")
        trial_id = entry["trial"].split("/")[1].rsplit("-", 1)[1]
        name = f"{pair.strip('-')}-{entry['variant'].lower()}-{trial_id}"
        meta = {key: entry[key] for key in ("pair", "variant", "turn_limit", "trial")}
        units.append(unit_rows(out_root, name, meta))
    extra = [
        unit_rows(out_root, "reference", {"pair": "reference", "variant": "—"}),
        unit_rows(
            out_root,
            "gemma-opencode-g0-5411de-r2",
            {"pair": "Gemma × opencode", "variant": "rerun"},
        ),
    ]

    print("### Units\n")
    table(
        [
            "Unit",
            "Cell",
            "Mutants",
            "Killed",
            "Survived",
            "Timeout",
            "No tests",
            "of which killed by suite",
            "Score",
            "Adjusted",
            "Timeout = survived",
            "On covered",
        ],
        [
            [
                u["unit"],
                f"{u['pair']} {u['variant']}",
                t["mutants"],
                t["killed"],
                t["survived"],
                t["timeout"],
                t["no_tests"],
                t["recheck_killed"],
                t["score"],
                t["corrected"],
                t["corrected_low"],
                t["covered"],
            ]
            for u in [*extra, *units]
            for t in [u["total"]]
        ],
    )
    print("### Layers\n")
    table(
        [
            "Unit",
            "Layer",
            "Mutants",
            "Killed",
            "Survived",
            "Timeout",
            "No tests",
            "of which killed by suite",
            "Score",
            "Adjusted",
            "On covered",
        ],
        [
            [
                u["unit"],
                row["layer"],
                row["mutants"],
                row["killed"],
                row["survived"],
                row["timeout"],
                row["no_tests"],
                row["recheck_killed"],
                row["score"],
                row["corrected"],
                row["covered"],
            ]
            for u in [*extra, *units]
            for row in u["layers"]
        ],
    )
    print("### Cells\n")
    cells: dict[tuple[str, str], list[dict]] = {}
    for u in units:
        cells.setdefault((u["pair"], u["variant"]), []).append(u)
    green: Counter[tuple[str, str]] = Counter()
    seen: Counter[tuple[str, str]] = Counter()
    for entry in inventory:
        if entry["place"] != "cell":
            continue
        key = (entry["pair"], entry["variant"])
        seen[key] += 1
        green[key] += entry["c4"].get("status") == "pass"
    rows = []
    for key in sorted(seen, key=lambda k: (k[1], k[0])):
        us = cells.get(key, [])
        rows.append(
            [
                key[0],
                key[1],
                f"{green[key]} of {seen[key]}",
                len(us),
                spread([u["total"]["score"] for u in us]),
                spread([u["total"]["corrected"] for u in us]),
                spread([u["total"]["covered"] for u in us]),
            ]
        )
    table(
        [
            "Pair",
            "Variant",
            "Green (C4)",
            "Measured",
            "Score: median (min–max)",
            "Adjusted",
            "On covered",
        ],
        rows,
    )
    print("### Layers by cell (corrected score, median)\n")
    names = ["domain", "application", "infrastructure", "api", "worker"]
    rows = []
    for key in sorted(cells, key=lambda k: (k[1], k[0])):
        row: list[object] = [key[0], key[1]]
        for name in names:
            values = [
                row["corrected"]
                for u in cells[key]
                for row in u["layers"]
                if row["layer"] == name and row["corrected"] is not None
            ]
            row.append(spread(values))
        rows.append(row)
    table(["Pair", "Variant", *names], rows)
    print("### Timeouts, load, time\n")
    table(
        [
            "Unit",
            "Mutants",
            "Timeouts in first run",
            "Left",
            "Share",
            "Timeouts in recheck",
            "Build failures (255)",
            "Peak load, shards",
            "Container-hours",
        ],
        [
            [
                u["unit"],
                t["mutants"],
                t["timeouts_first_run"],
                t["timeout"],
                f"{100 * t['timeout'] / t['mutants']:.1f} %".replace(".", ","),
                t["recheck_timeout"],
                t["crashes"],
                f"{fmt(u['peak_min'])}–{fmt(u['peak_max'])}",
                round(u["container_seconds"] / 3600, 1),
            ]
            for u in [*extra[1:], *units]
            for t in [u["total"]]
        ],
    )
    problems = [
        (u["unit"], u["total"]["recheck_missing"], u["bad_controls"])
        for u in [*extra, *units]
        if u["total"]["recheck_missing"] or u["bad_controls"]
    ]
    print(
        "recheck problems (unit, mutants without verdict, failed controls):", problems
    )
    print(
        "recheck exit codes:",
        sum((Counter(u["recheck_codes"]) for u in [*extra, *units]), Counter()),
    )
    batch = [u for u in [*extra[1:], *units] if u["unit"] not in STAGE1]
    hours = sum(u["container_seconds"] for u in batch) / 3600
    print("container-hours, batch:", round(hours, 1))
    if args.json:
        args.json.write_text(
            json.dumps({"units": units, "extra": extra}, indent=1, ensure_ascii=False)
            + "\n"
        )


if __name__ == "__main__":
    main()
