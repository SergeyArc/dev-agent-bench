"""Rubric loader and the deterministic step score.

The rubric is plain TOML: `bench/tests/rubric/common.toml` (scoring rules, the
common group, CJ1/CJ2) plus `bench/steps/step-<N>/tests/rubric.toml` (groups,
required-pass and judge criteria of the step). This module is the reference
implementation of the scoring rules stated at the top of `common.toml`.

Stdlib only (Python >= 3.11): it runs on the host, offline, on collected runs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomllib

SCHEMA = "dev-agent-bench/rubric/1"
STEPS = (1, 2, 3, 4, 5)
BENCH_DIR = Path(__file__).resolve().parent.parent


class RubricError(ValueError):
    """The rubric files are inconsistent."""


@dataclass(frozen=True)
class Group:
    id: str
    title: str
    weight: float
    criteria: dict[str, float]


@dataclass(frozen=True)
class JudgeCriterion:
    id: str
    title: str
    rule: str
    rule_text: str
    statement: str
    inputs: tuple[str, ...]
    scope: tuple[str, ...]
    anchors: dict[int, str]
    notes: dict[str, Any]
    samples: int
    from_step: int = 1

    @property
    def max_level(self) -> int:
        return max(self.anchors)


@dataclass(frozen=True)
class Rules:
    skipped: str
    error: str
    zero_end_reasons: frozenset[str]
    excluded_trial_status: frozenset[str]
    excluded_end_reasons: frozenset[str]


@dataclass(frozen=True)
class StepRubric:
    step: int
    name: str
    groups: tuple[Group, ...]
    required: frozenset[str]
    judges: tuple[JudgeCriterion, ...]
    rules: Rules
    unweighted: dict[str, str]
    post_run: frozenset[str]  # computed by bench/report from the snapshot, not by the verifier
    prompt_path: Path
    sha256: str

    @property
    def weights(self) -> dict[str, float]:
        return {cid: w for g in self.groups for cid, w in g.criteria.items()}


@dataclass
class StepScore:
    score: float | None  # None: the step is excluded (infrastructure failure)
    raw: float  # weighted mean before required-pass / end-reason zeroing
    zero_reasons: list[str] = field(default_factory=list)
    groups: dict[str, float] = field(default_factory=dict)
    not_passed: list[str] = field(default_factory=list)
    required_failed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # in the rubric, absent from criteria.json
    verifier_errors: list[str] = field(default_factory=list)  # status `error`: re-verify
    unknown: list[str] = field(default_factory=list)  # scored by the verifier, not in the rubric
    excluded: list[str] = field(default_factory=list)  # post-run criteria without a result
    flags: list[str] = field(default_factory=list)  # `no_snapshot`


def _judge(raw: dict[str, Any], defaults: dict[str, Any]) -> JudgeCriterion:
    anchors = {int(level): " ".join(text.split()) for level, text in raw["anchors"].items()}
    levels = int(raw.get("levels", defaults["levels"]))
    if sorted(anchors) != list(range(levels)):
        raise RubricError(f"{raw['id']}: anchors must be given for levels 0..{levels - 1}")
    return JudgeCriterion(
        id=raw["id"],
        title=raw["title"],
        rule=raw["rule"],
        rule_text=" ".join(raw["rule_text"].split()),
        statement=" ".join(raw["statement"].split()),
        inputs=tuple(raw["inputs"]),
        scope=tuple(raw["scope"]),
        anchors=anchors,
        notes=dict(raw.get("notes", {})),
        samples=int(raw.get("samples", defaults["samples"])),
        from_step=int(raw.get("from_step", 1)),
    )


def load(step: int, bench_dir: Path = BENCH_DIR) -> StepRubric:
    """Load and validate the rubric of one step."""
    common_path = bench_dir / "tests" / "rubric" / "common.toml"
    step_path = bench_dir / "steps" / f"step-{step}" / "tests" / "rubric.toml"
    common_bytes, step_bytes = common_path.read_bytes(), step_path.read_bytes()
    common, own = tomllib.loads(common_bytes.decode()), tomllib.loads(step_bytes.decode())
    for name, doc in ((common_path, common), (step_path, own)):
        if doc.get("schema") != SCHEMA:
            raise RubricError(f"{name}: schema must be {SCHEMA!r}")
    if own["step"] != step:
        raise RubricError(f"{step_path}: step = {own['step']}, expected {step}")

    groups = [Group(g["id"], g["title"], float(g["weight"]), dict(g["criteria"])) for g in own["group"]]
    cg = common["group"]
    groups.append(Group(cg["id"], cg["title"], float(cg["weight"]), dict(cg["criteria"])))

    seen: dict[str, str] = {}
    for group in groups:
        if group.weight <= 0 or not group.criteria:
            raise RubricError(f"step {step}: group {group.id} needs a positive weight and criteria")
        for cid, weight in group.criteria.items():
            if weight <= 0:
                raise RubricError(f"step {step}: {cid} needs a positive weight")
            if cid in seen:
                raise RubricError(f"step {step}: {cid} is in {seen[cid]} and in {group.id}")
            seen[cid] = group.id
    prefix = f"{step}."
    for cid, group_id in seen.items():
        if group_id != cg["id"] and not cid.startswith(prefix):
            raise RubricError(f"step {step}: {cid} is not a criterion of this step")

    required = frozenset(own["required"]["criteria"]) | frozenset(own["required"]["common"])
    if stray := sorted(required - seen.keys()):
        raise RubricError(f"step {step}: required-pass criteria without a weight: {stray}")

    defaults = common["judge_defaults"]
    judges = [_judge(j, defaults) for j in own.get("judge", [])]
    judges += [_judge(j, defaults) for j in common["judge"] if int(j.get("from_step", 1)) <= step]

    r = common["rules"]
    for key in ("skipped", "error"):
        if r[key] != "fail":
            raise RubricError(f"rules.{key} = {r[key]!r}: only 'fail' is implemented")
    post_run = frozenset(common.get("post_run", {}).get("criteria", []))
    if stray := sorted(post_run - seen.keys()):
        raise RubricError(f"step {step}: post_run names criteria without a weight: {stray}")
    if stray := sorted(post_run & required):
        raise RubricError(f"step {step}: a post-run criterion cannot be required-pass: {stray}")
    return StepRubric(
        step=step,
        name=own["name"],
        groups=tuple(groups),
        required=required,
        judges=tuple(judges),
        rules=Rules(
            skipped=r["skipped"],
            error=r["error"],
            zero_end_reasons=frozenset(r["zero_end_reasons"]),
            excluded_trial_status=frozenset(r["excluded_trial_status"]),
            excluded_end_reasons=frozenset(r["excluded_end_reasons"]),
        ),
        unweighted=dict(common.get("unweighted", {})),
        post_run=post_run,
        prompt_path=common_path.parent / defaults["prompt"],
        sha256=hashlib.sha256(common_bytes + b"\0" + step_bytes).hexdigest(),
    )


def score(rubric: StepRubric, criteria: list[dict[str, Any]], end_reason: str | None) -> StepScore:
    """Score one step from the `criteria` list of its criteria.json.

    The caller appends the post-run criteria (`rubric.post_run`) it could compute
    from the snapshot; those it could not compute are simply absent.
    """
    status = {c["id"]: c["status"] for c in criteria}
    weights = rubric.weights
    result = StepScore(score=0.0, raw=0.0)

    # A post-run criterion without a result was not measured: out of the denominator.
    result.excluded = sorted(rubric.post_run - status.keys())
    if result.excluded:
        result.flags.append("no_snapshot")

    total = sum(g.weight for g in rubric.groups)
    for group in rubric.groups:
        counted = {cid: w for cid, w in group.criteria.items() if cid not in result.excluded}
        passed = sum(w for cid, w in counted.items() if status.get(cid) == "pass")
        share = passed / sum(counted.values()) if counted else 0.0
        result.groups[group.id] = round(share, 4)
        result.raw += group.weight * share / total
    result.raw = round(result.raw, 4)

    for cid in weights:
        if cid in result.excluded:
            continue
        state = status.get(cid)
        if state is None:
            result.missing.append(cid)
        if state == "error":
            result.verifier_errors.append(cid)
        if state != "pass":
            result.not_passed.append(cid)
    result.required_failed = [cid for cid in result.not_passed if cid in rubric.required]
    # A criterion the verifier scored at this step (or as common) but the rubric does not know.
    result.unknown = sorted(
        c["id"]
        for c in criteria
        if c.get("scored") and c["id"] not in weights and int(c.get("step", 0)) in (0, rubric.step)
    )

    if end_reason in rubric.rules.excluded_end_reasons:
        result.score = None
        return result
    if result.required_failed:
        result.zero_reasons.append("required-pass: " + ", ".join(result.required_failed))
    if end_reason in rubric.rules.zero_end_reasons:
        result.zero_reasons.append(f"end_reason: {end_reason}")
    result.score = 0.0 if result.zero_reasons else result.raw
    return result


def regressions(step: int, history: dict[int, list[dict[str, Any]]]) -> list[str]:
    """Rule C5: criteria of earlier steps that passed at an earlier step and do not pass now.

    `history` maps a step number to the `criteria` list of its criteria.json.
    Common criteria are scored at every step and are not counted here.
    """
    passed_before: set[str] = set()
    for number, criteria in history.items():
        if number < step:
            passed_before |= {c["id"] for c in criteria if c["status"] == "pass"}
    return sorted(
        c["id"]
        for c in history.get(step, [])
        if 1 <= int(c.get("step", 0)) < step and c["status"] != "pass" and c["id"] in passed_before
    )
