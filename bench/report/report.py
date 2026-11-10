#!/usr/bin/env python3
"""Offline rubric report over collected runs.

    python3 bench/report/report.py ~/bench-runs/batch-… [more dirs] --out DIR \
        [--judge-dir DIR] [--quality]

Reads `run.json` and `steps/step-N/verifier/criteria.json` of every trial found
and writes `report.md` + `report.json` to `--out`:

  - the step score by the rubric (groups, required-pass, end-reason rule);
  - the post-run criteria N1 / N2 and the C3 recheck, both from the step snapshot
    (the recheck needs `--verifier-python`, see c3_recheck.py);
  - regressions (rule C5), reported next to the score, never inside it;
  - cells model x harness x task variant: median and range over the repeats, criteria matrix;
    a block of the same cell taken with another turn limit is a "harness setting" row, not a part
    of the cell;
  - `cells` in report.json: score, judge, reward, cost and criteria of every cell, and the criteria
    with the widest gap between the cells of the base variant;
  - process metrics from run.json (weight 0);
  - judge verdicts, when `--judge-dir` has them (see judge.py): the level of the first sample;
  - the Q/N layer from the step snapshots, with `--quality` (see quality.py).

Nothing is written into the run directories. No Docker, no network.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c3_recheck
import quality
import rubric as rubric_mod
import runs
from snapshots import Snapshot, SnapshotError

REPORT_SCHEMA = "dev-agent-bench/rubric-report/2"
GAPS_SHOWN = 10


def _mid(values: list[float], digits: int = 2) -> str:
    """Median and range: `0.84 [0.71–0.95]`."""
    if not values:
        return "—"
    med = statistics.median(values)
    fmt = f"{{:.{digits}f}}"
    if len(values) == 1:
        return fmt.format(med)
    return f"{fmt.format(med)} [{fmt.format(min(values))}–{fmt.format(max(values))}]"


def _id_key(cid: str) -> tuple[int, int, str]:
    head, _, tail = cid.partition(".")
    if head.isdigit() and tail.isdigit():
        return (int(head), int(tail), "")
    return (0, 0, cid)


def process_metrics(step: runs.StepRun) -> dict[str, Any]:
    record = step.run
    tokens = record.get("tokens") or {}
    turn_categories = record.get("turn_categories") or {}
    categories = turn_categories.get("categories") or {}
    g2 = record.get("g2") or {}
    return {
        "end_reason": step.end_reason,
        "turns": record.get("turns"),
        "completion_tokens": tokens.get("completion"),
        "reasoning_tokens": tokens.get("reasoning"),
        "wall_time_sec": record.get("wall_time_sec"),
        "peak_context_tokens": record.get("peak_context_tokens"),
        "check_calls": (categories.get("checks") or {}).get("tool_calls"),  # ruff, mypy, pytest, alembic
        "history_shrinks": record.get("history_shrinks"),
        "last_message_is_question": bool(record.get("last_message_is_question")),
        "last_response_length": bool(record.get("last_response_length")),
        "harness_signal": record.get("harness_signal"),
        # Answers cut by the output limit that called no tool: the step is degenerating.
        "output_limit_without_tool_call": (turn_categories.get("output_limit_hits") or {}).get("without_tool_call"),
        "turn_limit_hit": bool(record.get("turn_limit_hit")),
        "loop_detected": bool((record.get("loop_detected") or {}).get("detected")),
        "final_commit": g2.get("final_commit"),  # variants with the commit hook only
        "reward": (record.get("reward") or {}).get("reward"),  # the placeholder reward of the run
    }


def first_sample_level(verdict: dict[str, Any]) -> int | None:
    """The level the report uses: the first sample, also where the verdict holds three.

    The main judge run takes one sample per unit; when a unit has three samples, the median
    of three there would be a different measurement from the rest.
    """
    for sample in verdict.get("samples", []):
        if int(sample.get("n", 0)) == 1:
            level = sample.get("level")
            return int(level) if level is not None else None
    return None


def judge_verdicts(judge_dir: Path | None, trial: runs.Trial, step: int) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if judge_dir is None:
        return found
    for path in sorted((judge_dir / trial.name / f"step-{step}").glob("*.json")):
        verdict = json.loads(path.read_text())
        found[verdict["criterion"]] = verdict
    return found


def post_run(
    step: runs.StepRun, criteria: list[dict[str, Any]], verifier_python: Path | None, with_quality: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """What the report adds to the verifier's criteria from the snapshot of the step.

    Returns the criteria list to score and a record for the report:
      - N1 / N2 appended (absent when there is no snapshot: the rubric then leaves
        them out of the denominator);
      - C3 turned into `pass` when it failed on "Missing layer" only and the same
        check passes on the snapshot with `__init__.py` files added.
    """
    criteria = [dict(c) for c in criteria]
    info: dict[str, Any] = {"flags": []}
    c3 = next((c for c in criteria if c["id"] == "C3"), None)
    candidate = c3 is not None and c3["status"] == "fail" and c3_recheck.only_missing_layer(c3.get("evidence", ""))
    try:
        with Snapshot(step.dir, step.number) as snap:
            for cid, places in quality.suppressions(snap).items():
                evidence = f"{len(places)} found: " + ", ".join(places[:20]) if places else "none found"
                criteria.append(
                    {"id": cid, "status": "fail" if places else "pass", "evidence": evidence, "step": 0,
                     "scored": True, "source": "snapshot"}
                )  # fmt: skip
            if candidate and c3 is not None:
                info["c3"] = {"before": "fail", "after": "fail"}
                try:
                    again = c3_recheck.recheck(snap, verifier_python)
                except c3_recheck.RecheckUnavailable as exc:
                    info["flags"].append(f"c3_recheck_unavailable: {exc}")
                else:
                    info["c3"].update(after=again["status"], added=again["added"], evidence=again["evidence"])
                    if again["status"] == "pass":
                        c3.update(status="pass", evidence=again["evidence"], source="snapshot")
                        info["flags"].append(c3_recheck.FLAG)
            if with_quality:
                info["quality"] = quality.measure(snap)
    except SnapshotError as exc:
        info["snapshot_error"] = str(exc)
        if candidate:
            info["c3"] = {"before": "fail", "after": "fail"}
        if with_quality:
            info["quality"] = {"error": str(exc)}
    return criteria, info


def evaluate_trial(
    trial: runs.Trial,
    rubrics: dict[int, rubric_mod.StepRubric],
    judge_dir: Path | None,
    with_quality: bool,
    verifier_python: Path | None = None,
) -> dict[str, Any]:
    history = {n: (s.criteria or {}).get("criteria", []) for n, s in trial.steps.items()}
    steps: dict[int, dict[str, Any]] = {}
    for number, step in sorted(trial.steps.items()):
        rubric = rubrics[number]
        record: dict[str, Any] = {"process": process_metrics(step), "flags": []}
        if step.criteria is None:
            # No verifier output: nothing passed. Excluded only for an infrastructure failure.
            excluded = step.end_reason in rubric.rules.excluded_end_reasons
            record.update(score=None if excluded else 0.0, raw=0.0, zero_reasons=["no criteria.json"])
            record.update(groups={}, not_passed=sorted(rubric.weights, key=_id_key), required_failed=[])
            record.update(regressions=[], statuses={}, judge={}, judge_score=None)
            record["flags"].append("no criteria.json")
            steps[number] = record
            continue
        verifier_criteria = history[number]
        history[number], extra = post_run(step, verifier_criteria, verifier_python, with_quality)
        result = rubric_mod.score(rubric, history[number], step.end_reason)
        statuses = {c["id"]: c["status"] for c in history[number]}
        record.update(
            score=result.score,
            raw=result.raw,
            zero_reasons=result.zero_reasons,
            groups=result.groups,
            not_passed=sorted(result.not_passed, key=_id_key),
            required_failed=result.required_failed,
            regressions=rubric_mod.regressions(number, history),
            statuses=statuses,
            verifier_reward=step.criteria.get("reward"),
            c6=statuses.get("C6"),
            excluded_criteria=result.excluded,
        )
        record["flags"] += result.flags + extra["flags"]
        if "c3" in extra:
            # The same step scored with the verifier's own C3: what the recheck changed.
            as_verified = [c for c in history[number] if c["id"] != "C3"]
            as_verified += [c for c in verifier_criteria if c["id"] == "C3"]
            before = rubric_mod.score(rubric, as_verified, step.end_reason)
            record["c3_recheck"] = {**extra["c3"], "score_before": before.score, "raw_before": before.raw}
        if result.verifier_errors:
            record["flags"].append("verifier error (re-verify): " + ", ".join(result.verifier_errors))
        if result.missing:
            record["flags"].append("not in criteria.json: " + ", ".join(result.missing))
        if result.unknown:
            record["flags"].append("scored by the verifier, not in the rubric: " + ", ".join(result.unknown))
        if "snapshot_error" in extra:
            record["flags"].append(f"snapshot: {extra['snapshot_error']}")
        if step.end_reason not in ("completed", None):
            record["flags"].append(f"end_reason {step.end_reason}")
        for flag in ("last_message_is_question", "last_response_length"):
            if record["process"][flag]:
                record["flags"].append(flag)

        verdicts = judge_verdicts(judge_dir, trial, number)
        record["judge"] = {
            j.id: {
                "status": verdicts.get(j.id, {}).get("status", "not_run"),
                "level": first_sample_level(verdicts.get(j.id, {})),
                "max_level": j.max_level,
            }
            for j in rubric.judges
        }
        graded = [v["level"] / v["max_level"] for v in record["judge"].values() if v["level"] is not None]
        complete = len(graded) == len(rubric.judges) and graded
        record["judge_score"] = round(sum(graded) / len(graded), 4) if complete else None

        if "quality" in extra:
            record["quality"] = extra["quality"]
        steps[number] = record

    scored = [s["score"] for s in steps.values() if s["score"] is not None]
    excluded = trial.status in next(iter(rubrics.values())).rules.excluded_trial_status
    return {
        "name": trial.name,
        "dir": str(trial.dir),
        "model": trial.model,
        "harness": trial.harness,
        "variant": trial.variant,
        "max_turns": trial.max_turns,
        "status": trial.status,
        "run_source": trial.run_source,
        "excluded": excluded,
        "started_at": trial.run.get("started_at"),
        "repo_commit": (trial.run.get("repo") or {}).get("commit"),
        # Same aggregation as task.toml: multi_step_reward_strategy = "mean".
        "score": round(sum(scored) / len(scored), 4) if len(scored) == len(steps) and scored else None,
        "judge_score": _mean([s.get("judge_score") for s in steps.values()]),
        "reward": _mean([s["process"]["reward"] for s in steps.values()]),
        "steps": steps,
    }


def _mean(values: list[float | None]) -> float | None:
    """Mean over the steps of a trial; None when a step has no value."""
    known = [v for v in values if v is not None]
    return round(sum(known) / len(known), 4) if known and len(known) == len(values) else None


# ----------------------------------------------------------------------- cells

CellKey = tuple[str, str, str]


def group_cells(
    trials: list[dict[str, Any]],
) -> tuple[dict[CellKey, list[dict[str, Any]]], dict[tuple[str, str, str, int | None], list[dict[str, Any]]]]:
    """Cells model x harness x variant, and the blocks kept out of them.

    Blocks of one cell that differ in the turn limit of the harness were taken with different
    settings: the cell is the block with the highest limit, the others show what the setting does.
    """
    found: dict[CellKey, list[dict[str, Any]]] = defaultdict(list)
    for trial in trials:
        if not trial["excluded"]:
            found[(trial["model"], trial["harness"], trial["variant"])].append(trial)
    cells: dict[CellKey, list[dict[str, Any]]] = {}
    settings: dict[tuple[str, str, str, int | None], list[dict[str, Any]]] = {}
    for key, group in sorted(found.items()):
        limits = {t.get("max_turns") for t in group}
        top = max((x for x in limits if x is not None), default=None)
        cells[key] = [t for t in group if t.get("max_turns") == top]
        for limit in sorted(limits - {top}, key=lambda x: x or 0):
            settings[(*key, limit)] = [t for t in group if t.get("max_turns") == limit]
    return cells, settings


def _stats(values: list[float | None]) -> dict[str, Any]:
    known = [v for v in values if v is not None]
    if not known:
        return {"n": 0, "median": None, "min": None, "max": None, "values": []}
    return {"n": len(known), "median": round(statistics.median(known), 4), "min": min(known), "max": max(known), "values": known}  # fmt: skip


def cell_summary(group: list[dict[str, Any]], rubrics: dict[int, rubric_mod.StepRubric]) -> dict[str, Any]:
    """Everything the report says about a cell, from the evaluated trials of the cell."""
    steps = [(n, t["steps"][n]) for t in group for n in sorted(t["steps"])]
    procs = [s["process"] for _, s in steps]

    def per_trial(key: str) -> list[float | None]:
        totals: list[float | None] = []
        for trial in group:
            values = [s["process"].get(key) for s in trial["steps"].values()]
            totals.append(None if None in values else float(sum(values)))
        return totals

    def share(hit: list[bool]) -> dict[str, Any]:
        return {"steps": sum(hit), "of": len(hit), "share": round(sum(hit) / len(hit), 4) if hit else None}

    criteria: dict[str, dict[str, Any]] = {}
    for number in sorted(rubrics):
        for cid in rubrics[number].weights:
            # A criterion of the step counts at its step; a common one at every step.
            if cid[0].isdigit() and int(cid.split(".")[0]) != number:
                continue
            statuses = [
                s["statuses"].get(cid) for n, s in steps if n == number and cid not in s.get("excluded_criteria", [])
            ]
            entry = criteria.setdefault(cid, {"passed": 0, "of": 0})
            entry["passed"] += sum(status == "pass" for status in statuses)
            entry["of"] += len(statuses)
    for entry in criteria.values():
        entry["share"] = round(entry["passed"] / entry["of"], 4) if entry["of"] else None
    levels: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for _, step in steps:
        for cid, verdict in step.get("judge", {}).items():
            if verdict["level"] is not None:
                levels[cid].append((verdict["level"], verdict["max_level"]))
    judge_criteria = {
        cid: {
            "units": len(found),
            "mean_level": round(sum(level for level, _ in found) / len(found), 4),
            "max_level": found[0][1],
        }
        for cid, found in sorted(levels.items())
    }
    commits = [p["final_commit"] for p in procs if p.get("final_commit") is not None]
    first = group[0]
    return {
        "model": first["model"],
        "harness": first["harness"],
        "variant": first["variant"],
        "max_turns": first.get("max_turns"),
        "n": len(group),
        "trials": [t["name"] for t in group],
        "rubric": _stats([t["score"] for t in group]),
        "judge": _stats([t.get("judge_score") for t in group]),
        "reward": _stats([t.get("reward") for t in group]),
        "regressions_per_trial": _stats([float(sum(len(s["regressions"]) for s in t["steps"].values())) for t in group]),
        "cost": {
            "wall_time_sec_per_trial": _stats(per_trial("wall_time_sec")),
            "completion_tokens_per_trial": _stats(per_trial("completion_tokens")),
            "steps": len(procs),
            "timeout": share([p["end_reason"] == "timeout" for p in procs]),
            "crashed": share([p["end_reason"] == "crashed" for p in procs]),
            "degenerate": share([bool(p.get("output_limit_without_tool_call")) for p in procs]),
            "turn_limit_hit": share([bool(p.get("turn_limit_hit")) for p in procs]),
            "loop_detected": share([bool(p.get("loop_detected")) for p in procs]),
        },
        "final_commits": share([bool(c) for c in commits]) if commits else None,
        "criteria": criteria,
        "judge_criteria": judge_criteria,
    }


def criteria_gaps(cells: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Criteria ordered by the gap between the best and the worst cell of the base variant.

    Three repeats make many gaps equal (3/3 against 0/3): among equal gaps the criterion with the
    lower mean over the cells comes first.
    """
    base = [c for c in cells if c["variant"] == runs.BASE_VARIANT]

    def label(cell: dict[str, Any]) -> str:
        return f"{cell['model']} / {cell['harness']}"

    def ranked(values: dict[str, list[tuple[float, str]]]) -> list[dict[str, Any]]:
        rows = []
        for cid, found in values.items():
            if len(found) < 2:
                continue
            best, worst = max(found), min(found)
            mean = round(sum(value for value, _ in found) / len(found), 4)
            rows.append({"criterion": cid, "gap": round(best[0] - worst[0], 4), "best": best[0], "worst": worst[0], "mean": mean,
                         "best_cells": [name for value, name in found if value == best[0]],
                         "worst_cells": [name for value, name in found if value == worst[0]]})  # fmt: skip
        return sorted(rows, key=lambda r: (-r["gap"], r["mean"], _id_key(r["criterion"]), r["criterion"]))

    det: dict[str, list[tuple[float, str]]] = defaultdict(list)
    judged: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for cell in base:
        for cid, entry in cell["criteria"].items():
            if entry["share"] is not None:
                det[cid].append((entry["share"], label(cell)))
        for cid, entry in cell["judge_criteria"].items():
            judged[cid].append((entry["mean_level"], label(cell)))
    return {"deterministic": ranked(det), "judge": ranked(judged)}


def summarize(trials: list[dict[str, Any]], rubrics: dict[int, rubric_mod.StepRubric]) -> dict[str, Any]:
    cells, settings = group_cells(trials)
    summaries = [cell_summary(group, rubrics) for group in cells.values()]
    return {
        "cells": summaries,
        "harness_settings": [cell_summary(group, rubrics) for group in settings.values()],
        "criteria_gaps": criteria_gaps(summaries),
    }


# ------------------------------------------------------------------- rendering


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return [*lines, ""]


def _num(value: Any, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _range(stats: dict[str, Any], digits: int = 2, scale: float = 1.0) -> str:
    """Median and range of a `_stats` record, with the number of values when it is given."""
    return _mid([v / scale for v in stats["values"]], digits)


def _share(entry: dict[str, Any] | None) -> str:
    return "—" if not entry or not entry["of"] else f"{entry['steps']}/{entry['of']}"


def _cell_name(cell: dict[str, Any], variant: bool = True) -> list[str]:
    return [cell["model"], cell["harness"], *([cell["variant"]] if variant else [])]


def render_summary(summary: dict[str, Any]) -> list[str]:
    """The tables of the final report, from the `cells` of report.json."""
    cells: list[dict[str, Any]] = summary["cells"]
    base = [c for c in cells if c["variant"] == runs.BASE_VARIANT]
    ranked = sorted(base, key=lambda c: -(c["rubric"]["median"] if c["rubric"]["median"] is not None else -1.0))
    out = ["## Leaderboard: cells of the base variant", ""]
    out += ["Per trial: mean of the steps; per cell: median [min–max] over the trials. `judge n` is the number of", "trials with every judge verdict.", ""]  # fmt: skip
    out += _table(
        ["model", "harness", "n", "rubric", "judge", "judge n", "reward (placeholder)"],
        [[*_cell_name(c, False), str(c["n"]), _range(c["rubric"]), _range(c["judge"]), str(c["judge"]["n"]), _range(c["reward"])] for c in ranked],
    )  # fmt: skip

    def cost_row(cell: dict[str, Any]) -> list[str]:
        cost = cell["cost"]
        return [
            _range(cost["wall_time_sec_per_trial"], 0, 60.0),
            _range(cost["completion_tokens_per_trial"], 0, 1000.0),
            *[_share(cost[k]) for k in ("timeout", "crashed", "degenerate", "turn_limit_hit", "loop_detected")],
        ]

    cost_header = ["trial, min", "completion tokens per trial, thousands", "steps: timeout", "crashed",
                   "output limit without a tool call", "turn limit hit", "loop detected"]  # fmt: skip
    out += ["## Cost of a cell", ""]
    out += _table(["model", "harness", "variant", *cost_header], [[*_cell_name(c), *cost_row(c)] for c in ranked + [c for c in cells if c not in base]])  # fmt: skip

    out += ["## Model or harness (base variant)", ""]
    rows = []
    for model in sorted({c["model"] for c in base}):
        for cell in sorted((c for c in base if c["model"] == model), key=lambda c: c["harness"]):
            rows.append([model, cell["harness"], str(cell["n"]), _range(cell["rubric"]), _range(cell["judge"])])
    out += _table(["model", "harness", "n", "rubric", "judge"], rows)

    out += ["## Task variants", ""]
    rows = []
    varied = sorted({(c["model"], c["harness"]) for c in cells if c["variant"] != runs.BASE_VARIANT})
    for model, harness in varied:
        for cell in sorted((c for c in cells if (c["model"], c["harness"]) == (model, harness)), key=lambda c: c["variant"]):
            c1 = cell["criteria"].get("C1", {"passed": 0, "of": 0})
            rows.append([*_cell_name(cell), str(cell["n"]), _range(cell["rubric"]), _range(cell["judge"]),
                         f"{c1['passed']}/{c1['of']}", _share(cell["final_commits"])])  # fmt: skip
    out += _table(["model", "harness", "variant", "n", "rubric", "judge", "C1, steps passed", "steps with a final commit"], rows)

    settings = summary["harness_settings"]
    if settings:
        out += ["## Harness setting: blocks with another turn limit (not in the cells)", ""]
        rows = []
        for other in settings:
            same = [c for c in cells if _cell_name(c) == _cell_name(other)]
            for cell, where in [(other, "setting row"), *[(c, "cell") for c in same]]:
                rows.append([*_cell_name(cell), str(cell["max_turns"]), where, str(cell["n"]), _range(cell["rubric"]),
                             _range(cell["judge"]), _range(cell["reward"]), *cost_row(cell)])  # fmt: skip
        out += _table(["model", "harness", "variant", "turn limit", "", "n", "rubric", "judge", "reward (placeholder)", *cost_header], rows)

    names = [" / ".join(_cell_name(c, False)) for c in ranked]
    out += ["## Deterministic criteria by cell (base variant)", ""]
    out += ["Steps passed / steps counted; a criterion of a step counts at its step, a common one at every step.", "The least passed first.", ""]  # fmt: skip
    ids = sorted({cid for c in ranked for cid in c["criteria"]}, key=lambda cid: (_id_key(cid), cid))
    totals = {cid: (sum(c["criteria"].get(cid, {}).get("passed", 0) for c in ranked), sum(c["criteria"].get(cid, {}).get("of", 0) for c in ranked)) for cid in ids}  # fmt: skip
    ids.sort(key=lambda cid: totals[cid][0] / totals[cid][1] if totals[cid][1] else 0.0)
    out += _table(
        ["criterion", "all cells", *names],
        [[cid, f"{totals[cid][0]}/{totals[cid][1]}", *[f"{c['criteria'][cid]['passed']}/{c['criteria'][cid]['of']}" if cid in c["criteria"] else "—" for c in ranked]] for cid in ids],
    )  # fmt: skip
    out += ["## Judge criteria by cell (base variant): mean level", ""]
    judged = sorted({cid for c in ranked for cid in c["judge_criteria"]}, key=lambda cid: (_id_key(cid), cid))
    out += _table(
        ["criterion", *names],
        [[cid, *[f"{c['judge_criteria'][cid]['mean_level']:.2f} (n={c['judge_criteria'][cid]['units']})" if cid in c["judge_criteria"] else "—" for c in ranked]] for cid in judged],
    )  # fmt: skip

    out += [f"## Widest gaps between cells (base variant, top {GAPS_SHOWN})", ""]
    for kind, unit in (("deterministic", "share of steps passed"), ("judge", "mean level")):
        out += [f"{kind.capitalize()} criteria, {unit}:", ""]
        out += _table(
            ["criterion", "gap", "mean over cells", "best", "best cells", "worst", "worst cells"],
            [[g["criterion"], _num(g["gap"]), _num(g["mean"]), _num(g["best"]), "; ".join(g["best_cells"]), _num(g["worst"]), "; ".join(g["worst_cells"])] for g in summary["criteria_gaps"][kind][:GAPS_SHOWN]],
        )  # fmt: skip
    return out


def render(report: dict[str, Any], rubrics: dict[int, rubric_mod.StepRubric]) -> str:
    out: list[str] = ["# Rubric report", ""]
    trials = report["trials"]
    counted = [t for t in trials if not t["excluded"]]
    out += [
        (
            f"Trials found: {len(trials)}; in the statistics: {len(counted)}; "
            f"excluded (infrastructure failure): {len(trials) - len(counted)}."
        ),
        "",
        "The step score is the deterministic rubric score: weighted mean over the groups of the step and",
        "the common group, 0 when a required-pass criterion is not passed or the step hit its time limit.",
        "N1 / N2 come from the step snapshot (no snapshot: left out, flag `no_snapshot`);",
        "`c3_namespace_fix` marks a C3 turned into pass by the recheck below.",
        "Only criteria defined at the step and the common ones are scored; regressions of earlier criteria",
        "are a separate number. Judge criteria are a separate number as well. Differences of one criterion",
        "between cells of three repeats are not findings.",
        "",
    ]
    steps = sorted(rubrics)
    # A task variant is a different task: G0 and G2 of one model never share a cell.
    cells, settings = group_cells(trials)
    summary = report if "cells" in report else summarize(trials, rubrics)
    out += render_summary(summary)

    out += ["## Cells: median [min–max] over repeats", ""]
    rows = []
    for (model, harness, variant), group in sorted(cells.items()):
        row = [model, harness, variant, str(len(group))]
        for number in steps:
            row.append(_mid([t["steps"][number]["score"] for t in group if t["steps"].get(number, {}).get("score") is not None]))
        row.append(_mid([t["score"] for t in group if t["score"] is not None]))
        judged = [t["judge_score"] for t in group if t.get("judge_score") is not None]
        row.append(_mid(judged) if judged else "not run")
        row.append(_mid([float(sum(len(s["regressions"]) for s in t["steps"].values())) for t in group], 0))
        rows.append(row)
    header = ["model", "harness", "variant", "n", *[f"step {n} ({rubrics[n].name})" for n in steps], "trial (mean of steps)", "judge", "regressions per trial"]
    out += _table(header, rows)

    out += ["## Trials", ""]
    apart = {t["name"] for group in settings.values() for t in group}
    rows = []
    for trial in trials:
        for number, step in sorted(trial["steps"].items()):
            rows.append(
                [
                    trial["name"] + (" (excluded)" if trial["excluded"] else "") + (" (setting row)" if trial["name"] in apart else ""),
                    f"{trial['model']} / {trial['harness']} / {trial['variant']}",
                    str(number),
                    _num(step["score"]),
                    _num(step["raw"]),
                    "; ".join(step["zero_reasons"]) or "—",
                    ", ".join(step["not_passed"]) or "—",
                    ", ".join(step["regressions"]) or "—",
                    "; ".join(step["flags"]) or "—",
                ]
            )
    out += _table(
        ["trial", "cell", "step", "score", "before zeroing", "zeroed by", "not passed (own + common)", "regressions (C5)", "flags"],
        rows,
    )

    rechecked = [
        (trial, number, step)
        for trial in trials
        for number, step in sorted(trial["steps"].items())
        if "c3_recheck" in step
    ]
    if rechecked:
        out += [
            "## C3 recheck on the snapshot",
            "",
            "Steps where C3 failed on import-linter \"Missing layer\" only (a package directory without",
            "`__init__.py`): the verifier's C3 re-run on the snapshot with empty `__init__.py` files added.",
            "",
        ]
        out += _table(
            ["trial", "cell", "step", "C3 before", "C3 after", "score before → after", "`__init__.py` added"],
            [
                [
                    trial["name"],
                    f"{trial['model']} / {trial['harness']} / {trial['variant']}",
                    str(number),
                    step["c3_recheck"]["before"],
                    step["c3_recheck"]["after"],
                    f"{_num(step['c3_recheck']['score_before'])} → {_num(step['score'])}",
                    ", ".join(step["c3_recheck"].get("added", [])) or "—",
                ]
                for trial, number, step in rechecked
            ],
        )

    out += ["## Group scores", "", "Share of the group weight passed; per trial in the order of the trials table.", ""]
    for (model, harness, variant), group in sorted(cells.items()):
        out += [f"### {model} / {harness} / {variant}", ""]
        rows = []
        for number in steps:
            for g in rubrics[number].groups:
                values = [t["steps"][number]["groups"].get(g.id) for t in group if number in t["steps"]]
                values = [v for v in values if v is not None]
                rows.append([str(number), g.id, f"{g.weight:g}", " ".join(_num(v) for v in values), _mid(values)])
        out += _table(["step", "group", "weight", "repeats", "median [range]"], rows)

    out += [
        "## Criteria matrix",
        "",
        "`+` passed, `-` not passed, `s` skipped, `E` verifier error, `.` no result. One column per repeat:",
        "the criterion at the step that defines it (common criteria: at every step, steps left to right).",
        "`R` marks a required-pass criterion. `last` = passed at the last step of the trial.",
        "",
    ]
    mark = {"pass": "+", "fail": "-", "skipped": "s", "error": "E", None: "."}
    for (model, harness, variant), group in sorted(cells.items()):
        out += [f"### {model} / {harness} / {variant}", ""]
        rows = []
        for number in steps:
            rubric = rubrics[number]
            for cid in sorted((c for g in rubric.groups[:-1] for c in g.criteria), key=_id_key):
                own = [t["steps"].get(number, {}).get("statuses", {}).get(cid) for t in group]
                last = [t["steps"][max(t["steps"])]["statuses"].get(cid) for t in group]
                rows.append(
                    [
                        cid + (" R" if cid in rubric.required else ""),
                        " ".join(mark.get(s, "?") for s in own),
                        f"{sum(s == 'pass' for s in own)}/{len(own)}",
                        f"{sum(s == 'pass' for s in last)}/{len(last)}",
                    ]
                )
        common = rubrics[steps[0]].groups[-1]
        for cid in common.criteria:
            per_trial = ["".join(mark.get(t["steps"].get(n, {}).get("statuses", {}).get(cid), "?") for n in steps) for t in group]
            total = sum(c == "+" for cell in per_trial for c in cell)
            last = [t["steps"][max(t["steps"])]["statuses"].get(cid) for t in group]
            rows.append([cid, " ".join(per_trial), f"{total}/{sum(len(c) for c in per_trial)}", f"{sum(s == 'pass' for s in last)}/{len(last)}"])
        out += _table(["criterion", "repeats", "passed", "last"], rows)

    out += ["## Judge criteria", ""]
    judge_rows = []
    for trial in counted:
        for number, step in sorted(trial["steps"].items()):
            for cid, verdict in step.get("judge", {}).items():
                level = "—" if verdict["level"] is None else f"{verdict['level']}/{verdict['max_level']}"
                judge_rows.append([trial["name"], str(number), cid, verdict["status"], level])
    statuses: dict[str, int] = defaultdict(int)
    for row in judge_rows:
        statuses[row[3]] += 1
    out += [f"Units (trial x step x criterion): {len(judge_rows)}; by status: {dict(statuses)}.", ""]
    if any(row[3] in ("ok", "partial") for row in judge_rows):
        out += _table(["trial", "step", "criterion", "status", "level"], judge_rows)
    else:
        out += ["No verdicts: the judge was not run (dry run or no `--judge-dir`).", ""]

    out += ["## Process metrics (weight 0)", "", "Median [min–max] over the repeats of the cell.", ""]
    for (model, harness, variant), group in sorted(cells.items()):
        out += [f"### {model} / {harness} / {variant}", ""]
        rows = []
        for number in steps:
            procs = [t["steps"][number]["process"] for t in group if number in t["steps"]]

            def col(key: str, procs: list[dict[str, Any]] = procs) -> str:
                return _mid([float(p[key]) for p in procs if p.get(key) is not None], 0)

            reasons: dict[str, int] = defaultdict(int)
            for p in procs:
                reasons[str(p["end_reason"])] += 1
            rows.append(
                [
                    str(number),
                    col("turns"),
                    col("completion_tokens"),
                    col("reasoning_tokens"),
                    col("wall_time_sec"),
                    col("peak_context_tokens"),
                    col("check_calls"),
                    ", ".join(f"{k} x{v}" for k, v in sorted(reasons.items())),
                ]
            )
        out += _table(
            ["step", "turns", "completion tokens", "reasoning tokens", "wall time, s", "peak context, tokens", "check calls (ruff/mypy/pytest/alembic)", "end reasons"],
            rows,
        )

    if any("quality" in s for t in trials for s in t["steps"].values()):
        out += [
            "## Layer Q/N (diagnostic, weight 0)",
            "",
            "From the step snapshots. N1 `noqa`, N2 `type: ignore`, N3 `Any`, N4 `cast` — in `src` / in `tests`;",
            "N5 tool config outside the frozen files; N6 package stubs; N7 reflection in domain/application;",
            "Q1 source lines src / tests; Q2 cyclomatic complexity max / mean / functions above 10; Q3 function length max / p90.",
            "",
        ]
        rows = []
        for trial in counted:
            for number, step in sorted(trial["steps"].items()):
                q = step.get("quality")
                if not q:
                    continue
                if "error" in q:
                    rows.append([trial["name"], str(number), q["error"], *["—"] * 9])
                    continue
                rows.append(
                    [
                        trial["name"],
                        str(number),
                        *[f"{q['src'][k]} / {q['tests'][k]}" for k in ("N1", "N2", "N3", "N4")],
                        ", ".join(q["N5"]) or "0",
                        ", ".join(q["N6"]) or "0",
                        str(q["N7"]),
                        f"{q['Q1']['src']} / {q['Q1']['tests']}",
                        f"{q['Q2']['max']} / {q['Q2']['mean']} / {q['Q2']['above_10']}",
                        f"{q['Q3']['max']} / {q['Q3']['p90']}",
                    ]
                )
        out += _table(["trial", "step", "N1", "N2", "N3", "N4", "N5", "N6", "N7", "Q1", "Q2", "Q3"], rows)

    out += ["## Rubric", ""]
    out += _table(
        ["step", "required-pass", "groups (weight)", "judge criteria", "sha256"],
        [
            [
                f"{n} ({rubrics[n].name})",
                ", ".join(sorted(rubrics[n].required, key=_id_key)),
                ", ".join(f"{g.id} ({g.weight:g})" for g in rubrics[n].groups),
                ", ".join(j.id for j in rubrics[n].judges),
                rubrics[n].sha256[:12],
            ]
            for n in steps
        ],
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", type=Path, help="trial, job or batch directories")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--judge-dir", type=Path, default=None, help="verdicts written by judge.py")
    parser.add_argument("--quality", action="store_true", help="compute layer Q/N from the snapshots")
    parser.add_argument(
        "--verifier-python",
        type=Path,
        default=os.environ.get("BENCH_VERIFIER_PYTHON"),
        help="python of a virtualenv built from bench/environment/verifier/requirements.txt (C3 recheck)",
    )
    parser.add_argument("--bench-dir", type=Path, default=rubric_mod.BENCH_DIR)
    args = parser.parse_args(argv)

    rubrics = {step: rubric_mod.load(step, args.bench_dir) for step in rubric_mod.STEPS}
    trials = runs.discover(args.runs)
    if not trials:
        parser.error("no trial (run.json next to steps/) under the given directories")
    evaluated = [evaluate_trial(t, rubrics, args.judge_dir, args.quality, args.verifier_python) for t in trials]
    report = {
        "schema": REPORT_SCHEMA,
        "rubric_sha256": {str(n): r.sha256 for n, r in rubrics.items()},
        **summarize(evaluated, rubrics),
        "trials": evaluated,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    (args.out / "report.md").write_text(render(report, rubrics) + "\n")
    print(f"{len(trials)} trial(s) -> {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
