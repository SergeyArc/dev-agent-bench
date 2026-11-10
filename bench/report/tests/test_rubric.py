"""Self-test of the rubric files and of the scoring rules.

    python3 -m unittest discover -s bench/report/tests -v
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import c3_recheck
import cj1_rematch
import judge
import quality
import report
import rubric as rubric_mod
import runs
from snapshots import matches

CONTRACT = rubric_mod.BENCH_DIR.parent / "docs" / "contract.md"


def contract_ids() -> tuple[dict[int, set[str]], dict[int, set[str]], set[str]]:
    """([det] IDs per step, [judge] IDs per step, common [det] IDs) from contract §9."""
    text = CONTRACT.read_text().split("## 9. Criteria index", 1)[1]
    det: dict[int, set[str]] = {n: set() for n in rubric_mod.STEPS}
    jud: dict[int, set[str]] = {n: set() for n in rubric_mod.STEPS}
    common: set[str] = set()
    for cid, kind in re.findall(r"^- (\S+) `\[(det|judge)\]`", text, flags=re.MULTILINE):
        if cid[0].isdigit():
            (det if kind == "det" else jud)[int(cid.split(".")[0])].add(cid)
        elif kind == "det":
            common.add(cid)
    return det, jud, common


def criteria(rubric: rubric_mod.StepRubric, failed: frozenset[str] | set[str] = frozenset()) -> list[dict[str, object]]:
    return [
        {"id": cid, "status": "fail" if cid in failed else "pass", "scored": True,
         "step": int(cid.split(".")[0]) if cid[0].isdigit() else 0}
        for cid in rubric.weights
    ]  # fmt: skip


class RubricFiles(unittest.TestCase):
    def test_every_contract_criterion_has_a_weight(self) -> None:
        det, jud, common = contract_ids()
        for step in rubric_mod.STEPS:
            rubric = rubric_mod.load(step)
            weighted = set(rubric.weights)
            own = {c for c in weighted if c[0].isdigit()}
            self.assertEqual(own, det[step], f"step {step}: [det] criteria of contract §9")
            # C6 is a diagnostic without weight; C5 is a rule, not a criterion.
            self.assertEqual(weighted - own, common - {"C6"})
            self.assertEqual({j.id for j in rubric.judges if j.id[0].isdigit()}, jud[step])

    def test_group_weights_and_required(self) -> None:
        for step in rubric_mod.STEPS:
            rubric = rubric_mod.load(step)
            self.assertEqual(sum(g.weight for g in rubric.groups), 100)
            self.assertLessEqual(len(rubric.required), 4)
            self.assertFalse({"4.9", "4.10"} & rubric.required)
        self.assertEqual(rubric_mod.load(5).required, {"5.1", "5.3", "5.4", "5.5"})
        self.assertEqual({j.id for j in rubric_mod.load(1).judges}, {"1.J1", "1.J3", "CJ2"})
        self.assertIn("CJ1", {j.id for j in rubric_mod.load(2).judges})

    def test_judge_criteria_are_complete(self) -> None:
        for step in rubric_mod.STEPS:
            for criterion in rubric_mod.load(step).judges:
                self.assertEqual(sorted(criterion.anchors), [0, 1, 2], criterion.id)
                self.assertTrue(criterion.rule_text and criterion.scope and criterion.inputs, criterion.id)
                self.assertTrue(criterion.notes.get("do_not_judge"), criterion.id)
                self.assertEqual(criterion.samples, 3)


class Scoring(unittest.TestCase):
    def setUp(self) -> None:
        self.rubric = rubric_mod.load(4)

    def test_all_passed(self) -> None:
        result = rubric_mod.score(self.rubric, criteria(self.rubric), "completed")
        self.assertEqual(result.score, 1.0)
        self.assertFalse(result.not_passed)

    def test_weighted_mean_over_groups(self) -> None:
        # 4.10 is half of G-concurrency (20 of 100): 1 - 0.5 * 0.20.
        result = rubric_mod.score(self.rubric, criteria(self.rubric, {"4.10"}), "completed")
        self.assertAlmostEqual(result.score or 0, 0.9)
        self.assertEqual(result.groups["G-concurrency"], 0.5)

    def test_required_pass_zeroes_the_step(self) -> None:
        for cid in ("4.3", "C3"):
            result = rubric_mod.score(self.rubric, criteria(self.rubric, {cid}), "completed")
            self.assertEqual(result.score, 0.0)
            self.assertGreater(result.raw, 0.8)
            self.assertEqual(result.required_failed, [cid])

    def test_skipped_error_and_missing_are_not_passed(self) -> None:
        for status in ("skipped", "error"):
            rows = criteria(self.rubric)
            next(r for r in rows if r["id"] == "4.3")["status"] = status
            self.assertEqual(rubric_mod.score(self.rubric, rows, "completed").score, 0.0)
        rows = [r for r in criteria(self.rubric) if r["id"] != "4.11"]
        result = rubric_mod.score(self.rubric, rows, "completed")
        self.assertEqual(result.missing, ["4.11"])
        self.assertLess(result.score or 0, 1.0)

    def test_end_reasons(self) -> None:
        rows = criteria(self.rubric)
        self.assertEqual(rubric_mod.score(self.rubric, rows, "timeout").score, 0.0)
        self.assertIsNone(rubric_mod.score(self.rubric, rows, "infra_failure").score)
        for reason in ("crashed", "budget_exhausted"):
            self.assertEqual(rubric_mod.score(self.rubric, rows, reason).score, 1.0)

    def test_earlier_criteria_do_not_change_the_score(self) -> None:
        rows = [*criteria(self.rubric), {"id": "2.7", "status": "fail", "scored": True, "step": 2}]
        result = rubric_mod.score(self.rubric, rows, "completed")
        self.assertEqual(result.score, 1.0)
        self.assertFalse(result.unknown)

    def test_failed_c7_costs_only_its_own_weight(self) -> None:
        # C1 and C2 are checked by the verifier with the original configs: C7 voids nothing.
        result = rubric_mod.score(self.rubric, criteria(self.rubric, {"C7"}), "completed")
        self.assertEqual((result.flags, result.not_passed), ([], ["C7"]))
        # G-static is 25 of 100; C7 is 0.5 of its 7 weight units.
        self.assertAlmostEqual(result.score or 0, 1 - 0.25 * 0.5 / 7, places=4)

    def test_post_run_criteria(self) -> None:
        self.assertEqual(self.rubric.post_run, {"N1", "N2"})
        result = rubric_mod.score(self.rubric, criteria(self.rubric, {"N1"}), "completed")
        self.assertEqual(result.not_passed, ["N1"])
        self.assertAlmostEqual(result.score or 0, 1 - 0.25 * 0.5 / 7, places=4)
        # No snapshot: N1 / N2 have no result and leave the denominator — not a failure.
        rows = [r for r in criteria(self.rubric) if r["id"] not in ("N1", "N2")]
        result = rubric_mod.score(self.rubric, rows, "completed")
        self.assertEqual((result.score, result.flags, result.excluded), (1.0, ["no_snapshot"], ["N1", "N2"]))
        self.assertFalse(result.not_passed or result.missing)
        result = rubric_mod.score(self.rubric, [r for r in rows if r["id"] != "C1"] , "completed")
        self.assertAlmostEqual(result.groups["G-static"], 5 / 6, places=4)

    def test_regressions(self) -> None:
        history = {
            1: [{"id": "1.7", "status": "pass", "step": 1}, {"id": "1.12", "status": "fail", "step": 1},
                {"id": "C4", "status": "pass", "step": 0}],
            2: [{"id": "1.7", "status": "fail", "step": 1}, {"id": "1.12", "status": "fail", "step": 1},
                {"id": "C4", "status": "fail", "step": 0}, {"id": "2.1", "status": "fail", "step": 2}],
        }  # fmt: skip
        self.assertEqual(rubric_mod.regressions(2, history), ["1.7"])
        self.assertEqual(rubric_mod.regressions(1, history), [])


class JudgeAndQuality(unittest.TestCase):
    def test_prompt_is_built_without_touching_braces_in_code(self) -> None:
        rubric = rubric_mod.load(3)
        criterion = next(j for j in rubric.judges if j.id == "3.J3")
        code = 'x = {"a": 1}  # {materials} {id}'
        system, user = judge.build_prompt(criterion, 3, code, rubric.prompt_path.read_text())
        self.assertIn(code, user)
        self.assertIn('<level value="0">', user)
        self.assertNotIn("{anchors}", user)
        self.assertNotIn("---USER---", system + user)
        schema = judge.output_format(criterion.max_level)["schema"]
        self.assertEqual(schema["properties"]["level"]["enum"], [0, 1, 2])

    def test_scope_globs(self) -> None:
        scope = ("src/tracksvc/worker/**/*.py", "src/tracksvc/worker.py")
        self.assertTrue(matches("src/tracksvc/worker/consumer.py", scope))
        self.assertTrue(matches("src/tracksvc/worker.py", scope))
        self.assertFalse(matches("src/tracksvc/api/app.py", scope))

    def test_complexity_and_suppressions(self) -> None:
        source = (
            "from typing import Any, cast\n"
            "def f(x: Any) -> int:  # noqa: ANN401\n"
            "    if x and x > 1:\n"
            "        return cast(int, x)  # type: ignore[arg-type]\n"
            "    for i in range(3):\n"
            "        pass\n"
            "    return 0\n"
        )
        counts = quality._count_file(source)
        self.assertEqual((counts["N1"], counts["N2"], counts["N4"]), (1, 1, 1))
        self.assertEqual(counts["N3"], 1)  # the annotation; the import is not counted
        func = ast.parse(source).body[1]
        self.assertEqual(quality.complexity(func), 4)  # 1 + if + `and` + for
        self.assertEqual(quality._stubs(["alembic/env.py", "src/pydantic/__init__.py", "x.pyi"]), ["src/pydantic/", "x.pyi"])


class SnapshotRules(unittest.TestCase):
    BANNER = "import-linter:\n╔══╗─────────▶╔╗ ╔╗      ╔╗◀───┐\n      ╚╝────────────────────┘\n\n\n"

    def test_only_missing_layer(self) -> None:
        missing = self.BANNER + "Missing layer 'tracksvc.application': module tracksvc.application does not \nexist."
        self.assertTrue(c3_recheck.only_missing_layer(missing))
        for other in (
            missing + "\nsrc/tracksvc/application/x.py:4: application imports sqlalchemy.exc.IntegrityError",
            self.BANNER + "Syntax error in /app/src/tracksvc/api/app.py, line 111: EOF > \n/app/src/tracksvc/api/app.py",
            "import-linter:\nLayer rule (contract 2.3) BROKEN\n- tracksvc.domain.track -> tracksvc.application.errors (l.8)",
            "src/tracksvc/application/errors.py:7: application imports fastapi.status",
            "package tracksvc not found at /app/src/tracksvc",
            "",
        ):
            self.assertFalse(c3_recheck.only_missing_layer(other), other)

    def test_init_files_are_added_where_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            for rel in ("src/tracksvc/__init__.py", "src/tracksvc/api/app.py", "src/tracksvc/api/v1/routes.py"):
                (app / rel).parent.mkdir(parents=True, exist_ok=True)
                (app / rel).write_text("")
            added = c3_recheck.add_init_files(app)
            self.assertEqual(added, ["src/tracksvc/api/__init__.py", "src/tracksvc/api/v1/__init__.py"])
            self.assertEqual(c3_recheck.add_init_files(app), [])
        self.assertRegex(c3_recheck.pinned_import_linter(), r"^\d+\.\d+")

    def test_recomputed_run_record_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp) / "job" / "bench__x"
            (trial / "steps").mkdir(parents=True)
            record = {"status": "infra_failure", "model": {"key": "m"}, "harness": {"name": "h"}, "steps": []}
            (trial / "run.json").write_text(json.dumps(record))
            self.assertEqual(runs.discover([Path(tmp)])[0].status, "infra_failure")
            (trial / runs.RECOMPUTED).write_text(json.dumps({**record, "status": "ok", "recomputed_from": {}}))
            found = runs.discover([Path(tmp)])[0]
            self.assertEqual((found.status, found.run_source), ("ok", runs.RECOMPUTED))


class Cj1Rematch(unittest.TestCase):
    """The rename the verifier's detector does not see (reference solution, step 5)."""

    @staticmethod
    def suite_of(step: int) -> dict[str, str]:
        base = rubric_mod.BENCH_DIR / "steps" / f"step-{step}" / "solution" / "app"
        return {p.relative_to(base).as_posix(): p.read_text() for p in (base / "tests").rglob("*.py")}

    def test_renamed_and_edited_test_of_the_reference_is_recognised(self) -> None:
        collector = cj1_rematch.collector
        before, after = self.suite_of(4), self.suite_of(5)
        ids_before, ids_after = sorted(collector.analyse(before).tests), sorted(collector.analyse(after).tests)
        evidence = collector.compare(collector.Snapshot(before), collector.Snapshot(after), ids_before, ids_after)
        old = "tests/test_migrations.py::test_upgrade_head_creates_the_track_tables"
        self.assertEqual(evidence["missing_node_ids"], [old])  # the verifier's detector leaves it missing
        out = cj1_rematch.rematch(evidence, before, after)
        self.assertEqual(out["missing_node_ids"], [])
        pair = next(m for m in out["moved_or_renamed"] if m["from"] == old)
        self.assertEqual(pair["to"], "tests/test_migrations.py::test_upgrade_head_creates_the_tables")
        self.assertEqual(pair["matched_by"], "similar_body")
        self.assertGreater(pair["assertions"]["after"]["asserts"], pair["assertions"]["before"]["asserts"])
        self.assertEqual(evidence["missing_node_ids"], [old])  # the input is not modified

    def test_a_similar_body_alone_is_not_a_rename(self) -> None:
        arrange = "    track = client.post('/tracks', json=payload).json()\n"
        removed = arrange + (
            "    response = client.delete(f\"/tracks/{track['id']}/segments/alpha\")\n"
            "    assert response.status_code == 200\n    assert response.json()['segments'] == []\n"
        )
        added = arrange + (
            "    restart_service()\n    response = client.get(f\"/tracks/{track['id']}\")\n"
            "    assert response.status_code == 200\n    assert response.json() == track\n"
        )
        before = {"tests/test_a.py": "def test_remove_segment_success(client, payload):\n" + removed}
        after = {"tests/test_a.py": "def test_durability(client, payload, restart_service):\n" + added}
        evidence = {"missing_node_ids": ["tests/test_a.py::test_remove_segment_success"], "moved_or_renamed": []}
        old, new = cj1_rematch._functions(before), cj1_rematch._functions(after)
        likeness = cj1_rematch.similarity(*(cj1_rematch.body_tokens(f) for f in (*old.values(), *new.values())))
        self.assertGreater(likeness, cj1_rematch.BODY_WITH_NAME)  # alike by body, no word of the name in common
        out = cj1_rematch.rematch(evidence, before, after)
        self.assertEqual(out["missing_node_ids"], evidence["missing_node_ids"])
        self.assertEqual(out["rematch"]["pairs"], 0)
        # … but the same edit under a kept name is one test.
        after = {"tests/test_a.py": "def test_remove_segment_succeeds(client, payload, restart_service):\n" + added}
        self.assertEqual(cj1_rematch.rematch(evidence, before, after)["missing_node_ids"], [])


class JudgeCli(unittest.TestCase):
    def test_answer_is_parsed_strictly_on_the_level(self) -> None:
        good = '{"evidence": [{"file": "a.py", "lines": "1", "quote": "x"}], "summary": "s", "level": 2}'
        self.assertEqual(judge.parse_answer(good, 2)["level"], 2)
        self.assertTrue(judge.parse_answer(good, 2)["bare_json"])
        fenced = judge.parse_answer(f"```json\n{good}\n```", 2)
        self.assertEqual((fenced["level"], fenced["bare_json"]), (2, False))
        for bad in ("no json", '{"summary": "s"}', '{"level": 3}', '{"level": "2"}', '{"level": true}'):
            self.assertIn("error", judge.parse_answer(bad, 2), bad)

    def test_command_has_no_tools_and_replaces_the_system_prompt(self) -> None:
        command = judge.cli_command("claude", "some-model", "high", "SYSTEM")
        self.assertEqual(command[command.index("--system-prompt") + 1], "SYSTEM")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--model") + 1], "some-model")
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        for flag in ("--print", "--safe-mode", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, command)
        self.assertNotIn("--append-system-prompt", command)
        self.assertFalse([key for key in judge.CLI_ENV_PASSTHROUGH if key.startswith("ANTHROPIC")])
        # No model name is stored in the judge or in the rubric.
        sources = [judge.__file__, *map(str, (rubric_mod.BENCH_DIR / "tests" / "rubric").iterdir())]
        for path in sources:
            self.assertIsNone(re.search(r"claude-[a-z]+-\d|fable|opus|sonnet|haiku", Path(path).read_text(), re.IGNORECASE), path)

    def test_dry_run_summary(self) -> None:
        row = {"trial": "t", "model": "m", "harness": "h", "variant": "G0", "dir": "d", "step": 1, "samples": 3}
        rows = [
            {**row, "criterion": "1.J1", "status": "dry_run", "input_chars": 100},
            {**row, "criterion": "CJ2", "status": "dry_run", "input_chars": 300},
            {**row, "criterion": "1.J3", "status": "no_snapshot", "input_chars": None},
        ]
        entry = judge.summarize(rows)["trials"]["t"]
        self.assertEqual((entry["units"], entry["prompts"], entry["no_snapshot"], entry["samples"]), (3, 2, 1, 6))
        self.assertEqual((entry["chars_sum"], entry["chars_median"], entry["chars_max"]), (400, 200, 300))
        self.assertEqual(entry["variant"], "G0")


def write_trial(root: Path, job: str, variant: str | None = None, task: str | None = None) -> Path:
    """A minimal trial directory: one step, no verifier output."""
    trial = root / job / "bench__x"
    (trial / "steps" / "step-1").mkdir(parents=True)
    record: dict[str, object] = {
        "job_name": job, "status": "ok", "model": {"key": "m"}, "harness": {"name": "h"},
        "steps": [{"step": "step-1", "end_reason": "completed"}],
    }  # fmt: skip
    if variant:
        record["task_variant"] = {"variant": variant}
    (trial / "run.json").write_text(json.dumps(record))
    if task is not None:
        seen = root / job / "bench-run" / "task" / "bench" / "steps" / "step-1"
        seen.mkdir(parents=True)
        (seen / "task.md").write_text(task)
    return trial


class TaskVariant(unittest.TestCase):
    def test_variant_is_a_part_of_the_cell_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_trial(root, "base")
            write_trial(root, "gated", variant="g2")
            trials = runs.discover([root])  # both given together, same model and harness
            self.assertEqual([t.cell for t in trials], [("m", "h", "G0"), ("m", "h", "G2")])
            rubrics = {step: rubric_mod.load(step) for step in rubric_mod.STEPS}
            evaluated = [report.evaluate_trial(t, rubrics, None, False) for t in trials]
            text = report.render({"trials": evaluated}, rubrics)
        cells = text.split("## Cells", 1)[1].split("## Trials", 1)[0]
        self.assertIn("| m | h | G0 | 1 |", cells)
        self.assertIn("| m | h | G2 | 1 |", cells)
        self.assertNotIn("| 2 |", cells)  # not one cell of two repeats
        self.assertIn("### m / h / G2", text)

    def test_judge_gets_the_task_the_agent_saw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gated = runs.load_trial(write_trial(root, "gated", variant="g2", task="TASK OF G2"))
            self.assertEqual(gated.task_text(1, rubric_mod.BENCH_DIR), ("TASK OF G2", "trial"))
            base = runs.load_trial(write_trial(root, "base"))
            text, source = base.task_text(1, rubric_mod.BENCH_DIR)
            self.assertEqual(source, "repository")
            self.assertEqual(text, (rubric_mod.BENCH_DIR / "steps" / "step-1" / "task.md").read_text())


class FinalReportRules(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rubrics = {step: rubric_mod.load(step) for step in rubric_mod.STEPS}

    def trial(self, job: str, variant: str | None = None, max_turns: int | None = None) -> Path:
        path = write_trial(self.root / "runs", job, variant=variant)
        record = json.loads((path / "run.json").read_text())
        record["limits"] = {"max_turns": max_turns}
        (path / "run.json").write_text(json.dumps(record))
        return path

    def test_judge_level_is_the_first_sample(self) -> None:
        path = self.trial("sampled")
        (path / "steps" / "step-1" / "verifier").mkdir()
        (path / "steps" / "step-1" / "verifier" / "criteria.json").write_text(json.dumps({"criteria": []}))
        verdicts = self.root / "judge" / "sampled" / "step-1"
        verdicts.mkdir(parents=True)
        for judged in self.rubrics[1].judges:
            # A multi-sample unit: three samples, the verdict level is their median.
            samples = [{"n": 2, "level": 2}, {"n": 1, "level": 0}, {"n": 3, "level": 2}]
            verdict = {"criterion": judged.id, "status": "ok", "level": 2, "samples": samples}
            (verdicts / f"{judged.id}.json").write_text(json.dumps(verdict))
        evaluated = report.evaluate_trial(runs.load_trial(path), self.rubrics, self.root / "judge", False)
        self.assertEqual({v["level"] for v in evaluated["steps"][1]["judge"].values()}, {0})
        self.assertEqual(evaluated["judge_score"], 0.0)
        self.assertIsNone(report.first_sample_level({"status": "no_snapshot", "level": None, "samples": []}))
        # The one retry of an unread answer fills the slot of the first sample.
        self.assertEqual(report.first_sample_level({"samples": [{"n": 1, "level": 1, "retried": True}]}), 1)

    def test_a_block_with_another_turn_limit_is_not_in_the_cell(self) -> None:
        for job, variant, limit in (
            ("low-1", None, 300), ("high-1", None, 600), ("high-2", None, 600), ("hook", "g2", 300), ("rule", "G2b", 300),
        ):  # fmt: skip
            self.trial(job, variant, limit)
        trials = runs.discover([self.root / "runs"])
        evaluated = [report.evaluate_trial(t, self.rubrics, None, False) for t in trials]
        summary = report.summarize(evaluated, self.rubrics)
        cells = {(c["model"], c["harness"], c["variant"]): c for c in summary["cells"]}
        self.assertEqual(set(cells), {("m", "h", "G0"), ("m", "h", "G2"), ("m", "h", "G2b")})
        self.assertEqual(cells[("m", "h", "G0")]["trials"], ["high-1", "high-2"])
        self.assertEqual(cells[("m", "h", "G0")]["max_turns"], 600)
        self.assertEqual(cells[("m", "h", "G2")]["max_turns"], 300)  # the only block of its cell
        (apart,) = summary["harness_settings"]
        self.assertEqual((apart["trials"], apart["max_turns"], apart["variant"]), (["low-1"], 300, "G0"))
        text = report.render({"trials": evaluated}, self.rubrics)
        table = text.split("## Cells", 1)[1].split("## Trials", 1)[0]
        self.assertIn("| m | h | G0 | 2 |", table)
        self.assertIn("| m | h | G0 | 300 | setting row | 1 |", text)
        self.assertIn("low-1 (setting row)", text)

    def test_equal_gaps_are_ordered_by_the_mean_over_the_cells(self) -> None:
        def cell(harness: str, shares: dict[str, float]) -> dict[str, Any]:
            return {"model": "m", "harness": harness, "variant": "G0", "judge_criteria": {},
                    "criteria": {cid: {"share": share} for cid, share in shares.items()}}  # fmt: skip

        cells = [cell("a", {"1.1": 1.0, "1.2": 1.0, "1.3": 0.5}), cell("b", {"1.1": 1.0, "1.2": 0.0, "1.3": 0.5}),
                 cell("c", {"1.1": 0.0, "1.2": 0.0, "1.3": 0.5})]  # fmt: skip
        gaps = report.criteria_gaps([*cells, {**cell("d", {"1.3": 0.0}), "variant": "G2"}])["deterministic"]
        self.assertEqual([(g["criterion"], g["gap"]) for g in gaps], [("1.2", 1.0), ("1.1", 1.0), ("1.3", 0.0)])
        self.assertEqual((gaps[0]["best_cells"], gaps[0]["worst_cells"]), (["m / a"], ["m / b", "m / c"]))


class JudgeUnit(unittest.TestCase):
    """One unit judged with the call replaced by a list of prepared answers."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.rubric = rubric_mod.load(3)
        self.criterion = next(j for j in self.rubric.judges if j.id == "3.J3")
        self.trial = runs.Trial(dir=self.out, run={"job_name": "t", "model": {"key": "m"}, "harness": {"name": "h"}})
        self.answers: list[dict[str, Any]] = []
        self.code = "x = 1"
        for name, fake in (("materials", lambda *_: (self.code, [])), ("call_judge_cli", lambda *_: self.answers.pop(0))):
            patcher: Any = mock.patch.object(judge, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.target = self.out / "t" / "step-3" / "3.J3.json"

    def run_unit(
        self, samples: int, max_calls: int | None = None, model: str = "judge-model", effort: str = "high"
    ) -> tuple[dict[str, Any], argparse.Namespace]:
        args = argparse.Namespace(
            bench_dir=rubric_mod.BENCH_DIR, backend="claude-cli", samples=samples, out=self.out, execute=True,
            max_input_chars=10**6, model=model, effort=effort, claude_code_version="0", claude_bin="claude",
            max_calls=max_calls, calls_made=0, failed_in_a_row=0, stop="",
        )  # fmt: skip
        verdict = judge.judge_unit(self.criterion, self.trial, 3, self.rubric, args, None)
        if verdict["samples"] or verdict.get("failed_calls"):
            self.target.write_text(json.dumps(verdict))  # what main() does
        return verdict, args


class JudgeResume(JudgeUnit):
    """A second run goes on where the first one stopped; nothing paid for is asked again."""

    def test_samples_are_kept_and_topped_up(self) -> None:
        self.answers = [{"level": 2, "summary": "first"}]
        verdict, args = self.run_unit(1)
        self.assertEqual((verdict["status"], verdict["level"], args.calls_made), ("ok", 2, 1))
        verdict, args = self.run_unit(1)  # the same command again: no call
        self.assertEqual((verdict["status"], args.calls_made, self.answers), ("ok", 0, []))
        self.answers = [{"level": 0}, {"level": 0}]
        verdict, args = self.run_unit(3)  # `--samples 3` adds samples 2 and 3
        self.assertEqual((args.calls_made, verdict["status"], verdict["level"], verdict["spread"]), (2, "ok", 0, 2))
        self.assertEqual([(s["n"], s["level"]) for s in verdict["samples"]], [(1, 2), (2, 0), (3, 0)])
        self.assertEqual(verdict["samples"][0]["summary"], "first")

    def test_a_stopped_run_goes_on(self) -> None:
        self.answers = [{"level": 1}]
        verdict, args = self.run_unit(3, max_calls=1)
        self.assertEqual((verdict["status"], verdict["level"], bool(args.stop)), ("incomplete", None, True))
        self.assertEqual(json.loads(self.target.read_text())["samples"][0]["level"], 1)  # written call by call
        self.answers = [{"error": "claude -p failed: usage limit"}] * 3
        verdict, args = self.run_unit(3)  # the limit of the subscription: both calls fail, slots stay empty
        self.assertEqual((verdict["status"], len(verdict["samples"]), len(verdict["failed_calls"])), ("incomplete", 1, 2))
        self.assertEqual(args.failed_in_a_row, 2)
        self.answers = [{"level": 1}, {"level": 2}]
        verdict, args = self.run_unit(3)
        self.assertEqual((verdict["status"], verdict["level"], args.calls_made), ("ok", 1, 2))
        self.assertEqual(len(verdict["failed_calls"]), 2)  # kept for the record

    def test_three_failed_calls_in_a_row_stop_the_run(self) -> None:
        self.answers = [{"error": "claude -p failed"}, {"level": 2}, {"level": 2}]
        verdict, args = self.run_unit(3)  # one failure is not a reason to stop
        self.assertEqual((args.failed_in_a_row, args.stop, verdict["status"]), (0, "", "incomplete"))
        self.target.unlink()
        self.answers = [{"error": "e"}, {"error": "e"}, {"error": "e"}, {"level": 2}]
        verdict, args = self.run_unit(4)
        self.assertEqual((args.calls_made, len(self.answers)), (3, 1))  # the fourth call is not made
        self.assertIn("3 judge calls failed in a row", args.stop)
        self.assertEqual(verdict["status"], "incomplete")

    def test_a_rubric_edit_that_keeps_the_prompt_keeps_the_samples(self) -> None:
        self.answers = [{"level": 2}, {"level": 1}, {"level": 2}]
        first, _ = self.run_unit(3)
        edited = dataclasses.replace(self.rubric, sha256="0" * 64)  # e.g. a scoring rule removed
        with mock.patch.object(self, "rubric", edited):
            second, args = self.run_unit(3)
        self.assertEqual((args.calls_made, second["status"], second["level"]), (0, "ok", 2))
        self.assertEqual(first["prompt_sha256"], second["prompt_sha256"])
        self.assertEqual(second["rubric_sha256"], "0" * 64)  # the record of this run …
        self.assertEqual({s["rubric_sha256"] for s in second["samples"]}, {first["rubric_sha256"]})  # … and of the samples
        self.assertFalse(list(self.target.parent.glob("*.stale-*")))
        self.assertNotIn("rubric_sha256", judge.KEY_FIELDS)

    def test_a_verdict_for_another_prompt_is_moved_aside(self) -> None:
        self.answers = [{"level": 2}]
        first, _ = self.run_unit(1)
        self.code = "x = 2"  # the materials changed: another prompt
        self.answers = [{"level": 0}]
        second, args = self.run_unit(1)
        self.assertEqual((args.calls_made, second["level"]), (1, 0))
        self.assertNotEqual(first["prompt_sha256"], second["prompt_sha256"])
        stale = self.target.with_name(f"3.J3.json.stale-{str(first['prompt_sha256'])[:8]}")
        self.assertEqual(json.loads(stale.read_text())["level"], 2)

    def test_samples_of_another_judge_are_not_mixed_in(self) -> None:
        self.assertEqual(judge.KEY_FIELDS[-3:], ("backend", "model", "effort"))
        self.answers = [{"level": 2}]
        first, _ = self.run_unit(1)
        again, args = self.run_unit(1)  # the same judge: the sample is kept
        self.assertEqual((args.calls_made, again["level"]), (0, 2))
        self.answers = [{"level": 0}]
        second, args = self.run_unit(1, model="another-model")  # the same prompt, another model
        self.assertEqual((args.calls_made, second["level"], len(second["samples"])), (1, 0, 1))
        self.assertEqual(first["prompt_sha256"], second["prompt_sha256"])
        stale = self.target.with_name(f"3.J3.json.stale-{first['prompt_sha256'][:8]}-claude-cli-judge-model-high")
        self.assertEqual(json.loads(stale.read_text())["model"], "judge-model")
        self.answers = [{"level": 1}]
        third, args = self.run_unit(1, model="another-model", effort="low")  # … and another effort
        self.assertEqual((args.calls_made, third["level"]), (1, 1))
        self.assertEqual(len(list(self.target.parent.glob("*.stale-*"))), 2)


    def test_units_file(self) -> None:
        listed = [{"id": "U01", "trial": "t", "step": 3, "criterion": "3.J3"}]
        path = self.out / "units.json"
        path.write_text(json.dumps({"seed": 1, "units": listed}))
        self.assertEqual(judge.load_units(path), {("t", 3, "3.J3")})
        path.write_text(json.dumps(listed))
        self.assertEqual(judge.load_units(path), {("t", 3, "3.J3")})


class JudgeUnreadAnswer(JudgeUnit):
    """Tolerant reading, then one more call, then no verdict."""

    GOOD = '{"evidence": [{"file": "a.py", "lines": "1", "quote": "x"}], "summary": "s", "level": 2}'
    STRAY = '{"evidence": [{"file": "a.py", "lines": "1-2", "quote": "x = {}"}]}, "summary": "kept", "level": 1}'
    # The object is broken before `summary` (an unescaped quote), the verdict is written out.
    BROKEN = '{"evidence": [{"file": "a.py", "lines": "3", "quote": "print("a")"}], "summary": "so-so", "level": 1}'
    # Cut off before the verdict; the only `level` is inside a quote of `evidence`.
    QUOTED = '{"evidence": [{"file": "a.py", "lines": "7", "quote": "return {\\"level\\": 2}"}], "summary": "the code'
    UNESCAPED = '{"evidence": [{"file": "a.py", "lines": "7", "quote": "return {"level": 2}"}'

    def unread(self, text: str) -> dict[str, Any]:
        return judge.parse_answer(text, 2)

    def test_stray_brace_after_evidence_is_recovered(self) -> None:
        sample = self.unread(self.STRAY)
        self.assertEqual((sample["level"], sample["summary"], len(sample["evidence"])), (1, "kept", 1))
        self.assertEqual((sample["level_recovered"], sample["recovered_by"], sample["raw"]), (True, "stray_brace", self.STRAY))
        self.assertNotIn("level_recovered", self.unread(self.GOOD))  # the strict reading comes first

    def test_last_level_after_summary_is_recovered(self) -> None:
        sample = self.unread(self.BROKEN)
        self.assertEqual((sample["level"], sample["summary"], sample["recovered_by"]), (1, "so-so", "last_level"))
        two = self.BROKEN[:-1] + ', "note": "not \\"level\\": 0"} Final: {"level": 2}'
        self.assertEqual(self.unread(two)["level"], 2)  # the last one, and never the escaped one

    def test_nothing_is_recovered_without_a_level_after_summary(self) -> None:
        cases = (
            self.QUOTED,  # `level` only inside a quote of `evidence`, escaped
            self.UNESCAPED,  # the same, quotes not escaped: no `summary` before it
            '{"level": 2, "evidence": [}, "summary": "s"',  # a level before `summary` only
            '{"evidence": [}, "summary": "s", "level": 3}',  # out of range
            '{"evidence": [}, "summary": "s", "level": 1.5}',
            '{"evidence": [}, "summary": "s", "level": "2"}',
            'The level is 2.',
        )
        for text in cases:
            sample = self.unread(text)
            self.assertEqual((sample.get("error_kind"), sample.get("raw")), ("answer", text), text)
            self.assertNotIn("level", sample)

    def test_a_recovered_answer_is_not_asked_again(self) -> None:
        self.answers = [self.unread(self.STRAY), {"level": 1}, {"level": 1}]
        verdict, args = self.run_unit(3)
        self.assertEqual((verdict["status"], verdict["level"], args.calls_made), ("ok", 1, 3))
        self.assertEqual([bool(s.get("level_recovered")) for s in verdict["samples"]], [True, False, False])
        self.assertFalse(any("retried" in s for s in verdict["samples"]))

    def test_an_unread_answer_is_asked_once_more(self) -> None:
        self.answers = [self.unread(self.QUOTED), {"level": 0}, {"level": 2}, {"level": 2}]
        verdict, args = self.run_unit(3)
        self.assertEqual((verdict["status"], verdict["level"], args.calls_made, self.answers), ("ok", 2, 4, []))
        first = verdict["samples"][0]
        self.assertEqual((first["n"], first["level"], first["retried"]), (1, 0, True))
        self.assertEqual(first["first_answer"]["raw"], self.QUOTED)  # kept whole
        self.assertEqual(first["first_answer"]["error_kind"], "answer")
        self.assertEqual(args.failed_in_a_row, 0)
        verdict, args = self.run_unit(3)
        self.assertEqual((verdict["status"], args.calls_made), ("ok", 0))

    def test_unread_twice_is_left_without_a_level(self) -> None:
        self.answers = [self.unread(self.QUOTED), self.unread("no json"), {"level": 2}, {"level": 2}]
        verdict, args = self.run_unit(3)
        self.assertEqual((verdict["status"], verdict["level"], args.calls_made), ("partial", 2, 4))
        first = verdict["samples"][0]
        self.assertEqual((first["retried"], first["raw"], first["first_answer"]["raw"]), (True, "no json", self.QUOTED))
        self.assertNotIn("level", first)
        verdict, args = self.run_unit(3)  # never a third call for this sample
        self.assertEqual((verdict["status"], args.calls_made), ("partial", 0))
        self.target.unlink()
        self.answers = [self.unread("a"), self.unread("b")]
        verdict, args = self.run_unit(1)
        self.assertEqual((verdict["status"], verdict["level"], args.calls_made), ("error", None, 2))

    def test_the_retry_survives_a_stop(self) -> None:
        self.answers = [self.unread(self.QUOTED)]
        verdict, args = self.run_unit(1, max_calls=1)
        self.assertEqual((verdict["status"], bool(args.stop), len(verdict["samples"])), ("incomplete", True, 1))
        self.answers = [{"error": "claude -p failed: usage limit"}]
        verdict, args = self.run_unit(1)  # the retry itself failed as a call: still owed
        self.assertEqual((verdict["status"], args.calls_made, len(verdict["failed_calls"])), ("incomplete", 1, 1))
        self.answers = [{"level": 1}]
        verdict, args = self.run_unit(1)
        self.assertEqual((verdict["status"], verdict["level"], args.calls_made), ("ok", 1, 1))
        self.assertEqual((verdict["samples"][0]["retried"], verdict["samples"][0]["first_answer"]["raw"]), (True, self.QUOTED))

    def test_samples_stored_before_the_rule(self) -> None:
        """An unread sample of an earlier run: recovered from its stored text, else asked once more."""
        self.answers = [{"level": 2}, {"level": 2}, {"level": 2}]
        self.run_unit(3)
        stored = json.loads(self.target.read_text())
        old = {"error": "unparsable answer", "error_kind": "answer", "usage": {"output_tokens": 9}}
        stored["samples"][0] = {"n": 1, **old, "raw": self.STRAY}  # the whole answer was kept
        stored["samples"][2] = {"n": 3, **old, "raw": self.QUOTED[:40]}  # only its beginning was
        self.target.write_text(json.dumps(stored))
        self.answers = [{"level": 0}]
        verdict, args = self.run_unit(3)
        self.assertEqual((args.calls_made, verdict["status"]), (1, "ok"))
        one, two, three = verdict["samples"]
        self.assertEqual((one["level"], one["level_recovered"], one["usage"]), (1, True, {"output_tokens": 9}))
        self.assertNotIn("error", one)
        self.assertNotIn("retried", one)
        self.assertEqual((two["level"], "retried" in two), (2, False))
        self.assertEqual((three["level"], three["retried"], three["first_answer"]["raw"]), (0, True, self.QUOTED[:40]))


if __name__ == "__main__":
    unittest.main()
