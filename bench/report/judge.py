#!/usr/bin/env python3
"""LLM judge for the `[judge]` criteria of the rubric — offline, on collected runs.

Individual mode: one criterion per call, `samples` independent calls per
criterion (3), the verdict is the median level. The judge reads the step
snapshot (git bundle), never the container.

    # dry run (default): build every prompt, call nothing
    python3 bench/report/judge.py ~/bench-runs/batch-… --out /tmp/judge

    # real run: the model is a parameter and is never stored in the code
    BENCH_JUDGE_MODEL=<model> python3 bench/report/judge.py RUNS --out DIR --execute

Two backends (`--backend` / `BENCH_JUDGE_BACKEND`):

  claude-cli (default)  one `claude -p` process per sample, on the Claude Code
                        subscription — there is no API key. No tools, no MCP
                        servers, no skills, customizations off (`--safe-mode`), no
                        user / project / local settings (`--setting-sources ""`:
                        with them the user's language setting reached the judge),
                        the judge's system prompt REPLACES the one of Claude Code,
                        the process starts in an empty temporary directory and
                        gets a minimal environment (no API key variables). Without
                        tools the model has exactly one turn; a sample that
                        reports another number of turns is an error. Claude Code
                        still adds a few hundred tokens of its own to a request
                        (measured, content not visible from here).
  sdk                   the Anthropic SDK with a JSON-schema output format; needs
                        an API key in the environment.
  codex-cli             a judge of another vendor: one `codex exec` process per
                        sample, on the Codex subscription. Empty temporary working
                        directory, read-only sandbox, nothing persisted, no user
                        config, no rules, no AGENTS.md; the judge's system prompt
                        replaces the base instructions of Codex
                        (`model_instructions_file`); the same user message on
                        stdin, the same answer format and the same reading of the
                        answer. Codex keeps its shell tool: a sample during which a
                        command was run is an error. `--model default` /
                        `--effort default` leave the choice to Codex; the version
                        of Codex and the model it reports go into the sample.

Per criterion it writes `<out>/<trial>/step-N/<id>.json` (and `<id>.prompt.md`
in a dry run). `report.py --judge-dir <out>` picks the verdicts up.

A real run is resumable: run the same command again and it goes on where it
stopped (`--max-calls`, the usage limit of the subscription, three failed calls
in a row). The key of a sample is trial, step, criterion, sample number, the sha
of the prompt — the prompt is everything the judge sees (criterion, template,
materials) — and who judged: backend, model, effort. The sha of the rubric files
is written into the verdict as a record and is not a part of the key — a rubric
edit that leaves the prompt as it was keeps the samples. `--samples 3` after a run with `--samples 1` adds samples 2
and 3 and leaves sample 1 as it is. A call that failed (no answer) leaves the
slot empty for the next run. A verdict written for another prompt or by another
judge (backend, model, effort) is moved aside (`<id>.json.stale-<sha>[-<judge>]`),
never overwritten and never mixed with the samples of this run.

An answer that is not the JSON object asked for, in this
order:
  1. tolerant reading, by two rules fixed in advance: the known slip — a stray
     `}` right after the `evidence` array; then the last `"level": N` that
     stands after the `"summary"` key. Success: the sample has a level and the
     flag `level_recovered`.
  2. otherwise ONE more call with the same prompt — a new independent sample,
     flag `retried`; the unread answer is kept whole under `first_answer`.
  3. unread again: the sample has no level and is not asked any more.

`--units FILE` limits a run to the listed units (`trial`, `step`, `criterion`),
for example the calibration sample.

No refusal fallback is configured on purpose: a fallback would silently replace
the judge model, and the judge model is part of the measurement. A refused or
failed sample is recorded as such; a criterion with fewer than two valid
samples has no verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cj1_rematch
import rubric as rubric_mod
import runs
from snapshots import Snapshot, SnapshotError, matches

VERDICT_SCHEMA_ID = "dev-agent-bench/judge-verdict/1"
MAX_OUTPUT_TOKENS = 16000
# Characters, not tokens: a guard against sending a runaway snapshot. A unit above
# the limit is reported as `too_large` and never truncated.
DEFAULT_MAX_INPUT_CHARS = 600_000
BACKENDS = ("claude-cli", "sdk", "codex-cli")
# One process per sample, no schema-constrained output: the same system prompt, format note included.
CLI_BACKENDS = ("claude-cli", "codex-cli")
CLI_TIMEOUT_SEC = 1800
# The CLI backend has no schema-constrained output: the answer format is stated instead.
CLI_FORMAT_NOTE = (
    "Output the JSON object and nothing else: no text before or after it, no Markdown code fence."
)
# The way to the network is the caller's: both spellings, tools read either.
PROXY_ENV = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)  # fmt: skip
# What a `claude -p` judge process inherits. Nothing of the calling session, and no API
# key variable: the call must go through the subscription login of Claude Code.
CLI_ENV_PASSTHROUGH = (
    "HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG", "LC_ALL", "TMPDIR", "TERM",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
    *PROXY_ENV, "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
)  # fmt: skip


CODEX_ENV_PASSTHROUGH = (
    "HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG", "LC_ALL", "TMPDIR", "TERM", "CODEX_HOME",
    *PROXY_ENV, "SSL_CERT_FILE",
)  # fmt: skip
CODEX_DEFAULT = "default"  # model / effort left to Codex


MAX_FAILED_IN_A_ROW = 3
# What makes a stored sample reusable: the same unit, judged on the same text by the same
# judge. With the sample number (`n`) this is the resume key; the rubric sha is a record,
# not a part of it.
KEY_FIELDS = ("trial", "step", "criterion", "prompt_sha256", "backend", "model", "effort")
# Written into every sample: what the verdict said when the sample was taken.
SAMPLE_RECORD = ("rubric_sha256", "backend", "model", "effort", "claude_code_version", "codex_version")
# A JSON key outside a string value: inside one the quotes are escaped.
_SUMMARY_KEY = re.compile(r'(?<!\\)"summary"\s*:')
_LEVEL_KEY = re.compile(r'(?<!\\)"level"\s*:\s*(\d+)(?![\d.])')
_STRAY_BRACE = re.compile(r'\]\s*\}(?=\s*,\s*"summary"\s*:)')
_JSON_STRING = re.compile(r'\s*("(?:[^"\\]|\\.)*")')


def _numbered(text: str) -> str:
    return "\n".join(f"{n:4d}  {line}" for n, line in enumerate(text.splitlines(), 1))


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- (none)"


def output_format(max_level: int) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "lines": {"type": "string"},
                            "quote": {"type": "string"},
                        },
                        "required": ["file", "lines", "quote"],
                        "additionalProperties": False,
                    },
                },
                "summary": {"type": "string"},
                "level": {"type": "integer", "enum": list(range(max_level + 1))},
            },
            "required": ["evidence", "summary", "level"],
            "additionalProperties": False,
        },
    }


@dataclass(frozen=True)
class Part:
    """One piece of the materials, as it is before it is wrapped for the prompt."""

    kind: str  # file | no_files | step_diff | tests_diff | verifier_evidence | task
    name: str  # path inside the snapshot for `file`, otherwise empty
    text: str

    def render(self) -> str:
        if self.kind == "file":
            return f'<file path="{self.name}">\n{_numbered(self.text)}\n</file>'
        if self.kind == "no_files":
            return f"<files>{self.text}</files>"
        tag = {
            "step_diff": "diff_since_previous_step",
            "tests_diff": "tests_diff_since_previous_step",
            "verifier_evidence": "verifier_evidence",
            "task": "task_of_this_step",
        }[self.kind]
        return f"<{tag}>\n{self.text}\n</{tag}>"


def material_parts(
    criterion: rubric_mod.JudgeCriterion,
    trial: runs.Trial,
    step: int,
    bench_dir: Path,
) -> tuple[list[Part], list[str]]:
    """The pieces of the materials and notes about what could not be supplied."""
    step_run = trial.steps[step]
    parts: list[Part] = []
    notes: list[str] = []
    needs_snapshot = {"snapshot", "step_diff", "tests_diff"} & set(criterion.inputs)

    evidence: dict[str, Any] | None = None
    evidence_path = step_run.dir / "verifier" / "cj1-evidence.json"
    if evidence_path.is_file():
        evidence = json.loads(evidence_path.read_text())

    snapshot: Snapshot | None = None
    if needs_snapshot:
        try:
            snapshot = Snapshot(step_run.dir, step).__enter__()
        except SnapshotError as exc:
            notes.append(str(exc))
            # Without the code there is nothing to judge; only CJ1 can fall back
            # to the test diff the verifier collected.
            if {"snapshot", "step_diff"} & set(criterion.inputs):
                return [], notes
    try:
        base = prev_commit = None
        if snapshot is not None:
            previous = trial.steps.get(step - 1)
            prev_commit = ((previous.criteria or {}).get("snapshot") or {}).get("commit") if previous else None
            base = prev_commit or snapshot.initial_commit()
        for kind in criterion.inputs:
            if kind == "snapshot" and snapshot is not None:
                paths = [p for p in snapshot.files() if matches(p, criterion.scope)]
                parts += [Part("file", path, snapshot.read(path)) for path in paths]
                if not paths:
                    parts.append(Part("no_files", "", "No file of the snapshot matches the scope of this criterion."))
            elif kind == "step_diff" and snapshot is not None and base:
                parts.append(Part("step_diff", "", snapshot.diff(base, criterion.scope) or "(no changes)"))
            elif kind == "tests_diff":
                if snapshot is not None and base:
                    diff = snapshot.diff(base, criterion.scope)
                elif evidence is not None:
                    diff = str(evidence.get("diff", ""))
                    notes.append("tests diff taken from cj1-evidence.json (no snapshot bundle)")
                else:
                    continue
                parts.append(Part("tests_diff", "", diff or "(no changes)"))
            elif kind == "cj1_evidence":
                if evidence is None:
                    notes.append("no cj1-evidence.json for this step")
                    continue
                shown = {k: v for k, v in evidence.items() if k != "diff"}
                if snapshot is not None and prev_commit:
                    # Renamed-and-edited tests the verifier's detector left under `missing_node_ids`.
                    shown = cj1_rematch.rematch(
                        shown, _test_files(snapshot, prev_commit), _test_files(snapshot, None)
                    )
                else:
                    shown["rematch"] = {"status": "not_run", "reason": "no snapshot of this or of the previous step"}
                    notes.append("cj1 evidence not rematched (no snapshot of this or of the previous step)")
                parts.append(Part("verifier_evidence", "", json.dumps(shown, indent=2, ensure_ascii=False)))
            elif kind == "task":
                # The task as the agent saw it: a task variant (G2, …) words the step differently.
                task, source = trial.task_text(step, bench_dir)
                if source != "trial" and trial.variant != runs.BASE_VARIANT:
                    notes.append(f"task.md of variant {trial.variant} not found next to the trial: repository text used")
                parts.append(Part("task", "", task))
    finally:
        if snapshot is not None:
            snapshot.__exit__(None, None, None)
    return parts, notes


def materials(
    criterion: rubric_mod.JudgeCriterion,
    trial: runs.Trial,
    step: int,
    bench_dir: Path,
) -> tuple[str, list[str]]:
    """The materials block of the prompt and notes about what could not be supplied."""
    parts, notes = material_parts(criterion, trial, step, bench_dir)
    return "\n\n".join(part.render() for part in parts), notes


def _test_files(snapshot: Snapshot, rev: str | None) -> dict[str, str]:
    return {p: snapshot.read(p, rev) for p in snapshot.files(rev) if p.startswith("tests/") and p.endswith(".py")}


def build_prompt(
    criterion: rubric_mod.JudgeCriterion, step: int, material_text: str, template: str
) -> tuple[str, str]:
    """(system, user) for one criterion."""
    system, user = template.split("---USER---\n", 1)
    extra_keys = [k for k in criterion.notes if k not in ("accept", "do_not_judge")]
    extra_lines = []
    for key in extra_keys:
        value = criterion.notes[key]
        text = _bullets(value) if isinstance(value, list) else str(value)
        extra_lines.append(f"<{key}>\n{text}\n</{key}>")
    fields = {
        "{id}": criterion.id,
        "{title}": criterion.title,
        "{statement}": criterion.statement,
        "{rule}": criterion.rule,
        "{rule_text}": criterion.rule_text,
        "{anchors}": "\n".join(f'<level value="{n}">{text}</level>' for n, text in sorted(criterion.anchors.items())),
        "{accept}": _bullets(criterion.notes.get("accept", [])),
        "{do_not_judge}": _bullets(criterion.notes.get("do_not_judge", [])),
        "{extra}": "\n".join(extra_lines),
        "{step}": str(step),
    }
    for key, value in fields.items():
        user = user.replace(key, value)
    # Last, so that braces inside the agent's code are never treated as placeholders.
    user = user.replace("{materials}", material_text)
    return system.strip(), user


def call_judge(client: Any, model: str, effort: str, system: str, user: str, max_level: int) -> dict[str, Any]:
    """One sample. Returns {level, summary, evidence} or {error}."""
    anthropic = importlib.import_module("anthropic")  # needed only for a real run of this backend

    try:
        with client.messages.stream(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            cache_control={"type": "ephemeral"},
            output_config={"effort": effort, "format": output_format(max_level)},
        ) as stream:
            message = stream.get_final_message()
    except anthropic.RateLimitError as exc:
        return {"error": f"rate limited: {exc.message}"}
    except anthropic.APIStatusError as exc:
        return {"error": f"API error {exc.status_code}: {exc.message}"}
    except anthropic.APIConnectionError as exc:
        return {"error": f"connection error: {exc}"}

    usage = message.usage.model_dump() if message.usage else {}
    if message.stop_reason != "end_turn":
        # The model answered and the answer is unusable (refusal, cut off): the slot is spent.
        error = {"error": f"stop_reason {message.stop_reason}", "error_kind": "answer"}
        return {**error, "served_model": message.model, "usage": usage}
    text = next((block.text for block in message.content if block.type == "text"), "")
    return {**parse_answer(text, max_level), "served_model": message.model, "usage": usage}


def _strict(text: str, max_level: int) -> dict[str, Any] | None:
    """{level, summary, evidence} when the text holds the JSON object asked for."""
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start : end + 1])
        level = data["level"]
    except (ValueError, KeyError, TypeError):
        return None
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= max_level:
        return None
    evidence = data.get("evidence", [])
    return {
        "level": level,
        "summary": str(data.get("summary", "")),
        "evidence": evidence if isinstance(evidence, list) else [],
        "bare_json": start == 0 and end == len(text.rstrip()) - 1,  # nothing around the object
    }


def recover(text: str, max_level: int) -> dict[str, Any] | None:
    """Tolerant reading of an answer the strict one rejected; None when no rule applies.

    Two recovery rules, in this order. Nothing else is tried: a level
    is never guessed from the wording of the answer.
    """
    # 1. The known slip: `"evidence": [...]}, "summary": …` — the object closed too early.
    found = list(_STRAY_BRACE.finditer(text))
    if found:
        last = found[-1]
        fixed = _strict(text[: last.start()] + "]" + text[last.end() :], max_level)
        if fixed is not None:
            return {**fixed, "bare_json": False, "level_recovered": True, "recovered_by": "stray_brace", "raw": text}
    # 2. The last `"level": N` after the `"summary"` key. Keys are matched outside string
    # values only, so a `level` quoted inside `evidence` (escaped quotes, and before
    # `summary`) is never taken for the verdict.
    summaries = list(_SUMMARY_KEY.finditer(text))
    if not summaries:
        return None
    levels = list(_LEVEL_KEY.finditer(text, summaries[-1].end()))
    if not levels or not 0 <= int(levels[-1].group(1)) <= max_level:
        return None
    summary = ""
    quoted = _JSON_STRING.match(text, summaries[-1].end())
    if quoted:
        try:
            summary = str(json.loads(quoted.group(1)))
        except ValueError:
            pass
    return {
        "level": int(levels[-1].group(1)),
        "summary": summary,
        "evidence": [],  # not read: the object is broken somewhere before `summary`
        "bare_json": False,
        "level_recovered": True,
        "recovered_by": "last_level",
        "raw": text,
    }


def parse_answer(text: str, max_level: int) -> dict[str, Any]:
    """{level, summary, evidence} from the judge's answer, or {error, error_kind, raw}.

    Strict reading first, then the tolerant one (`recover`, flag `level_recovered`).
    `error_kind = "answer"`: the judge answered and no level can be read. The whole
    answer is kept in `raw`; the caller asks once more (`retried`).
    """
    parsed = _strict(text, max_level) or recover(text, max_level)
    if parsed is not None:
        return parsed
    return {"error": "unparsable answer", "error_kind": "answer", "raw": text}


def cli_version(binary: str) -> str:
    done = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"`{binary} --version` failed: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def cli_command(binary: str, model: str, effort: str, system: str) -> list[str]:
    """One judge call through Claude Code (flags of `claude --help`, version 2.1.x)."""
    return [
        binary,
        "--print",  # non-interactive: the prompt comes from stdin, one answer, exit
        "--model", model,
        "--effort", effort,
        "--system-prompt", system,  # replaces the system prompt of Claude Code
        "--tools", "",  # no built-in tools
        "--strict-mcp-config",  # no MCP servers (none is passed)
        "--disable-slash-commands",  # no skills
        "--safe-mode",  # CLAUDE.md, plugins, hooks, custom agents, output styles: all off
        "--setting-sources", "",  # no user / project / local settings (they add e.g. a language rule)
        "--no-session-persistence",
        "--output-format", "json",
    ]  # fmt: skip


def call_judge_cli(binary: str, model: str, effort: str, system: str, user: str, max_level: int) -> dict[str, Any]:
    """One sample through `claude -p`. Returns {level, summary, evidence, …} or {error, …}."""
    env = {key: os.environ[key] for key in CLI_ENV_PASSTHROUGH if key in os.environ}
    with tempfile.TemporaryDirectory(prefix="bench-judge-") as empty:  # no CLAUDE.md / AGENTS.md / settings
        try:
            done = subprocess.run(
                cli_command(binary, model, effort, system),
                input=user, capture_output=True, text=True, check=False, cwd=empty, env=env,
                timeout=CLI_TIMEOUT_SEC,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return {"error": f"claude -p timed out after {CLI_TIMEOUT_SEC} s"}
    try:
        reply = dict(json.loads(done.stdout))
    except ValueError:
        tail = (done.stderr or done.stdout).strip()[-600:]
        return {"error": f"claude -p exited with {done.returncode}, no JSON result", "raw": tail}
    usage = reply.get("usage") or {}
    served = sorted(reply.get("modelUsage") or {})
    sample: dict[str, Any] = {
        "served_model": served[0] if len(served) == 1 else served,
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        },
        "model_usage": reply.get("modelUsage"),
        "duration_ms": reply.get("duration_ms"),
        "duration_api_ms": reply.get("duration_api_ms"),
        "num_turns": reply.get("num_turns"),
        "stop_reason": reply.get("stop_reason"),
    }
    if done.returncode != 0 or reply.get("is_error") or reply.get("subtype") != "success":
        detail = str(reply.get("result") or reply.get("subtype") or done.stderr.strip())[:600]
        return {**sample, "error": f"claude -p failed: {detail}"}
    if len(served) != 1:
        return {**sample, "error": f"expected one model in the answer, got {served}"}
    if reply.get("num_turns") != 1:
        return {**sample, "error": f"expected one turn, got {reply.get('num_turns')}"}
    return {**parse_answer(str(reply.get("result", "")), max_level), **sample}


def codex_command(binary: str, model: str, effort: str, system_file: Path, answer_file: Path, cwd: Path) -> list[str]:
    """One judge call through Codex (flags of `codex exec --help`, codex-cli 0.160)."""
    command = [
        binary, "exec",
        "--sandbox", "read-only",  # commands of the model could only read
        "--skip-git-repo-check",  # the working directory is an empty temporary one
        "--ephemeral",  # no session files
        "--ignore-user-config",  # no $CODEX_HOME/config.toml; the login is still used
        "--ignore-rules",
        "--color", "never",
        "--json",  # events on stdout: usage, model, what the agent did
        "-C", str(cwd),
        "-c", f"model_instructions_file={json.dumps(str(system_file))}",  # replaces the base instructions
        "-c", "project_doc_max_bytes=0",  # no AGENTS.md of the user or of a project
        "-o", str(answer_file),
    ]  # fmt: skip
    if model != CODEX_DEFAULT:
        command += ["--model", model]
    if effort != CODEX_DEFAULT:
        command += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
    return [*command, "-"]  # the prompt comes from stdin


def _find_key(node: Any, key: str) -> Any:
    """The first value stored under `key` anywhere in a decoded JSON value."""
    if isinstance(node, dict):
        if node.get(key) is not None:
            return node[key]
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            found = _find_key(child, key)
            if found is not None:
                return found
    return None


def read_codex_events(stdout: str) -> dict[str, Any]:
    """What the JSONL events of `codex exec --json` say about a call; unknown events are only counted."""
    types: dict[str, int] = {}
    items: dict[str, int] = {}
    usage: Any = None
    model: Any = None
    errors: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type"))
        types[kind] = types.get(kind, 0) + 1
        item = event.get("item")
        if kind == "item.completed" and isinstance(item, dict):
            item_kind = str(item.get("type"))
            items[item_kind] = items.get(item_kind, 0) + 1
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        model = model or _find_key(event, "model")
        if "error" in kind or "failed" in kind:
            errors.append(json.dumps(event, ensure_ascii=False)[:600])
    return {
        "served_model": model if isinstance(model, str) else None,  # None: Codex did not report it
        "usage": usage,
        "events": types,
        "items": items,
        "num_turns": types.get("turn.completed", 0),
        "event_errors": errors,
    }


def call_judge_codex(binary: str, model: str, effort: str, system: str, user: str, max_level: int) -> dict[str, Any]:
    """One sample through `codex exec`. Returns {level, summary, evidence, …} or {error, …}."""
    env = {key: os.environ[key] for key in CODEX_ENV_PASSTHROUGH if key in os.environ}
    with (
        tempfile.TemporaryDirectory(prefix="bench-judge-") as empty,  # nothing to read, nothing to obey
        tempfile.TemporaryDirectory(prefix="bench-judge-io-") as io_dir,
    ):
        system_file, answer_file = Path(io_dir) / "system.md", Path(io_dir) / "answer.txt"
        system_file.write_text(system)
        try:
            done = subprocess.run(
                codex_command(binary, model, effort, system_file, answer_file, Path(empty)),
                input=user, capture_output=True, text=True, check=False, cwd=empty, env=env,
                timeout=CLI_TIMEOUT_SEC,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return {"error": f"codex exec timed out after {CLI_TIMEOUT_SEC} s"}
        answer = answer_file.read_text() if answer_file.is_file() else ""
    sample = read_codex_events(done.stdout)
    errors = sample.pop("event_errors")
    if done.returncode != 0 or not answer.strip():
        detail = "; ".join(errors) or (done.stderr or done.stdout).strip()[-600:]
        return {**sample, "error": f"codex exec failed (exit {done.returncode}): {detail}"}
    # Without a way to switch the shell tool off, the single turn is checked after the fact.
    acted = {k: v for k, v in sample["items"].items() if k not in ("agent_message", "reasoning")}
    if acted or sample["num_turns"] != 1:
        return {**sample, "error": f"expected one turn without tool use, got turns {sample['num_turns']}, items {acted}",
                "error_kind": "answer", "raw": answer}  # fmt: skip
    return {**parse_answer(answer, max_level), **sample}


def call_backend(args: argparse.Namespace, client: Any, system: str, user: str, max_level: int) -> dict[str, Any]:
    if args.backend == "claude-cli":
        return call_judge_cli(args.claude_bin, args.model, args.effort, system, user, max_level)
    if args.backend == "codex-cli":
        return call_judge_codex(args.codex_bin, args.model, args.effort, system, user, max_level)
    return call_judge(client, args.model, args.effort, system, user, max_level)


def unread(sample: dict[str, Any] | None) -> bool:
    """The judge answered and no level could be read from the answer."""
    return sample is not None and "level" not in sample and sample.get("error_kind") == "answer"


def is_final(sample: dict[str, Any] | None) -> bool:
    """A sample that fills its slot: a level, or an unread answer after the one retry."""
    return sample is not None and ("level" in sample or (unread(sample) and bool(sample.get("retried"))))


def same_key(old: dict[str, Any], new: dict[str, Any]) -> bool:
    return all(old.get(name) == new.get(name) for name in KEY_FIELDS)


def settle(verdict: dict[str, Any], samples: int) -> None:
    """Status and level of a verdict from the samples it holds."""
    levels = [s["level"] for s in verdict["samples"] if "level" in s]
    final = [s for s in verdict["samples"] if is_final(s)]
    verdict["samples_requested"] = samples = max(samples, len(verdict["samples"]))
    verdict["level"] = None
    verdict.pop("spread", None)
    if len(final) < samples:
        verdict["status"] = "incomplete"  # stopped or a call failed: run again to fill the slots
    elif len(levels) >= min(2, samples):
        verdict["level"] = statistics.median_low(levels)
        verdict["spread"] = max(levels) - min(levels)
        verdict["status"] = "ok" if len(levels) == samples else "partial"
    else:
        verdict["status"] = "error"


def resume(verdict: dict[str, Any], target: Path) -> None:
    """Take over the samples of an earlier run of this unit; move a verdict of another prompt aside."""
    try:
        old = dict(json.loads(target.read_text()))
    except (OSError, ValueError):
        return
    if not old.get("samples") and not old.get("failed_calls"):
        return  # a dry-run record: nothing was paid for
    if not same_key(old, verdict):
        stale = target.with_name(f"{target.name}.stale-{str(old.get('prompt_sha256'))[:8]}")
        what = "prompt"
        if old.get("prompt_sha256") == verdict.get("prompt_sha256"):
            # The same text judged by another judge: the name says by whom.
            judged_by = "-".join(str(old.get(k)) for k in ("backend", "model", "effort"))
            stale = stale.with_name(f"{stale.name}-{re.sub(r'[^A-Za-z0-9._-]+', '_', judged_by)}")
            what = "judge (backend, model, effort)"
        if stale.exists():
            raise RuntimeError(f"{stale} exists: not replacing a verdict that was moved aside earlier")
        target.rename(stale)
        verdict["notes"].append(f"a verdict for another {what} was moved to {stale.name}")
        return
    verdict["failed_calls"] = old.get("failed_calls", [])
    for sample in old["samples"]:
        # What the sample was taken under stays with the sample: the verdict's own fields are of this run.
        sample = {**{k: old[k] for k in SAMPLE_RECORD if k in old}, **sample}
        if unread(sample) and not sample.get("retried"):
            # Kept from before the tolerant reading, or the run stopped before the retry.
            found = recover(str(sample.get("raw", "")), int(verdict["max_level"]))
            if found is not None:
                sample = {k: v for k, v in sample.items() if k not in ("error", "error_kind")} | found
        if "level" in sample or unread(sample):
            verdict["samples"].append(sample)
        else:
            verdict["failed_calls"].append(sample)


def judge_unit(
    criterion: rubric_mod.JudgeCriterion,
    trial: runs.Trial,
    step: int,
    step_rubric: rubric_mod.StepRubric,
    args: argparse.Namespace,
    client: Any,
) -> dict[str, Any]:
    material_text, notes = materials(criterion, trial, step, args.bench_dir)
    verdict: dict[str, Any] = {
        "schema": VERDICT_SCHEMA_ID,
        "criterion": criterion.id,
        "step": step,
        "trial": trial.name,
        "variant": trial.variant,
        "max_level": criterion.max_level,
        "samples_requested": criterion.samples,
        "rubric_sha256": step_rubric.sha256,
        "notes": notes,
        "level": None,
        "samples": [],
    }
    if not material_text:
        verdict["status"] = "no_snapshot" if notes else "no_materials"
        return verdict
    system, user = build_prompt(criterion, step, material_text, step_rubric.prompt_path.read_text())
    if args.backend in CLI_BACKENDS:
        system = f"{system}\n\n{CLI_FORMAT_NOTE}"
    samples = args.samples or criterion.samples
    verdict["backend"] = args.backend
    verdict["samples_requested"] = samples
    verdict["prompt_sha256"] = hashlib.sha256((system + "\0" + user).encode()).hexdigest()
    verdict["input_chars"] = len(system) + len(user)
    out_dir = args.out / trial.name / f"step-{step}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if verdict["input_chars"] > args.max_input_chars:
        verdict["status"] = "too_large"
        return verdict
    if not args.execute:
        (out_dir / f"{criterion.id}.prompt.md").write_text(f"# system\n\n{system}\n\n# user\n\n{user}")
        verdict["status"] = "dry_run"
        return verdict

    verdict["model"] = args.model
    verdict["effort"] = args.effort
    if args.backend == "claude-cli":
        verdict["claude_code_version"] = args.claude_code_version
    elif args.backend == "codex-cli":
        verdict["codex_version"] = args.codex_version
    target = out_dir / f"{criterion.id}.json"
    resume(verdict, target)
    verdict["calls_made"] = 0  # by this run; the samples say what each of them cost
    record = {k: verdict[k] for k in SAMPLE_RECORD if k in verdict}

    def ask() -> dict[str, Any] | None:
        """One call, counted and written to disk; None when the run has to stop before it."""
        if args.max_calls is not None and args.calls_made >= args.max_calls:
            args.stop = f"--max-calls {args.max_calls} reached"
        if args.stop:
            return None
        args.calls_made += 1
        verdict["calls_made"] += 1
        sample = call_backend(args, client, system, user, criterion.max_level)
        # A usage limit or a broken setup fails every call: stop instead of burning the queue.
        # An unread answer is an answer: with its retry it must not look like two failed calls.
        args.failed_in_a_row = 0 if "level" in sample or unread(sample) else args.failed_in_a_row + 1
        if args.failed_in_a_row >= MAX_FAILED_IN_A_ROW:
            args.stop = f"{MAX_FAILED_IN_A_ROW} judge calls failed in a row: {sample['error']}"
        return sample

    def keep(n: int, sample: dict[str, Any]) -> None:
        """Put an answer into slot `n`; a call without an answer goes to `failed_calls`."""
        held = slots.get(n)
        if "level" not in sample and not unread(sample):
            verdict.setdefault("failed_calls", []).append(sample)
        elif held is None:
            slots[n] = {"n": n, **record, **sample}
        else:  # the one retry of an unread answer: a new independent sample, the first answer kept whole
            first = {k: v for k, v in held.items() if k != "n"}
            slots[n] = {"n": n, **record, **sample, "retried": True, "first_answer": first}
        verdict["samples"] = [slots[k] for k in sorted(slots)]
        settle(verdict, samples)
        target.write_text(json.dumps(verdict, indent=2, ensure_ascii=False) + "\n")  # nothing paid for is lost

    slots: dict[int, dict[str, Any]] = {int(s["n"]): s for s in verdict["samples"]}
    for _ in range(sum(not is_final(slots.get(n)) for n in range(1, samples + 1))):
        n = next(n for n in range(1, samples + 1) if not is_final(slots.get(n)))
        for _attempt in range(1 if unread(slots.get(n)) else 2):  # an unread answer is asked once more
            sample = ask()
            if sample is None:
                break
            keep(n, sample)
            if not unread(slots.get(n)) or is_final(slots.get(n)) or not unread(sample):
                break
        if args.stop:
            break
    verdict["samples"] = [slots[k] for k in sorted(slots)]
    settle(verdict, samples)
    return verdict


def load_units(path: Path) -> set[tuple[str, int, str]]:
    """Units of a `--units` file: a list of {trial, step, criterion}, bare or under `units`."""
    data = json.loads(path.read_text())
    listed = data["units"] if isinstance(data, dict) else data
    return {(str(u["trial"]), int(u["step"]), str(u["criterion"])) for u in listed}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Volume of a run per trial: units, prompts built, units without a snapshot, prompt characters."""
    trials: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = trials.setdefault(
            row["trial"],
            {"model": row["model"], "harness": row["harness"], "variant": row["variant"], "dir": row["dir"],
             "units": 0, "prompts": 0,
             "no_snapshot": 0, "other": 0, "chars": [], "samples": 0},
        )  # fmt: skip
        entry["units"] += 1
        if row.get("input_chars") and row["status"] != "too_large":
            entry["prompts"] += 1
            entry["chars"].append(row["input_chars"])
            entry["samples"] += row["samples"]
        elif row["status"] == "no_snapshot":
            entry["no_snapshot"] += 1
        else:
            entry["other"] += 1
    for entry in trials.values():
        chars = entry.pop("chars")
        entry["chars_sum"] = sum(chars)
        entry["chars_median"] = int(statistics.median(chars)) if chars else 0
        entry["chars_max"] = max(chars, default=0)
    return {"trials": trials, "units": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", type=Path, help="trial, job or batch directories")
    parser.add_argument("--out", type=Path, required=True, help="directory for verdicts")
    parser.add_argument("--execute", action="store_true", help="call the judge model (default: dry run)")
    parser.add_argument("--backend", choices=BACKENDS, default=os.environ.get("BENCH_JUDGE_BACKEND", BACKENDS[0]))
    parser.add_argument("--model", default=os.environ.get("BENCH_JUDGE_MODEL"), help="judge model (ID or alias)")
    parser.add_argument("--effort", default=os.environ.get("BENCH_JUDGE_EFFORT", "high"))
    parser.add_argument("--claude-bin", default=os.environ.get("BENCH_JUDGE_CLAUDE_BIN", "claude"))
    parser.add_argument("--codex-bin", default=os.environ.get("BENCH_JUDGE_CODEX_BIN", "codex"))
    parser.add_argument("--samples", type=int, default=None, help="calls per criterion (default: the rubric's, 3)")
    parser.add_argument("--max-calls", type=int, default=None, help="stop after this many judge calls")
    parser.add_argument("--steps", default="1,2,3,4,5")
    parser.add_argument("--criteria", default="", help="comma-separated IDs; default: all")
    parser.add_argument("--units", type=Path, default=None, help="JSON list of {trial, step, criterion}: judge only these")
    parser.add_argument("--max-input-chars", type=int, default=DEFAULT_MAX_INPUT_CHARS)
    parser.add_argument("--bench-dir", type=Path, default=rubric_mod.BENCH_DIR)
    args = parser.parse_args(argv)
    if args.samples is not None and args.samples < 1:
        parser.error("--samples must be at least 1")
    args.calls_made, args.failed_in_a_row, args.stop = 0, 0, ""

    client = None
    if args.execute:
        if not args.model:
            parser.error("--execute needs --model or BENCH_JUDGE_MODEL")
        if args.backend == "claude-cli":
            args.claude_code_version = cli_version(args.claude_bin)
        elif args.backend == "codex-cli":
            args.codex_version = cli_version(args.codex_bin)
        else:
            client = importlib.import_module("anthropic").Anthropic()

    steps = [int(s) for s in args.steps.split(",") if s]
    only = {c for c in args.criteria.split(",") if c}
    units = load_units(args.units) if args.units else None
    rubrics = {step: rubric_mod.load(step, args.bench_dir) for step in steps}
    counts: dict[str, int] = {}
    chars = 0
    rows: list[dict[str, Any]] = []
    for trial in runs.discover(args.runs):
        if trial.status in rubrics[steps[0]].rules.excluded_trial_status:
            continue
        for step in steps:
            if step not in trial.steps:
                continue
            for criterion in rubrics[step].judges:
                if args.stop or (only and criterion.id not in only):
                    continue
                if units is not None and (trial.name, step, criterion.id) not in units:
                    continue
                target = args.out / trial.name / f"step-{step}" / f"{criterion.id}.json"
                if not args.execute and target.is_file() and json.loads(target.read_text()).get("samples"):
                    print(f"{trial.name} step-{step} {criterion.id}: kept (a dry run never replaces a real verdict)")
                    continue
                verdict = judge_unit(criterion, trial, step, rubrics[step], args, client)
                if verdict["status"] == "incomplete" and not verdict["samples"] and not verdict.get("failed_calls"):
                    continue  # stopped before the first call of this unit: nothing to record
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(verdict, indent=2, ensure_ascii=False) + "\n")
                counts[verdict["status"]] = counts.get(verdict["status"], 0) + 1
                chars += verdict.get("input_chars", 0)
                rows.append(
                    {"trial": trial.name, "model": trial.model, "harness": trial.harness, "variant": trial.variant,
                     "dir": str(trial.dir), "step": step, "criterion": criterion.id, "status": verdict["status"],
                     "input_chars": verdict.get("input_chars"), "samples": args.samples or criterion.samples}
                )  # fmt: skip
                kept = " (kept, no call)" if args.execute and verdict.get("calls_made") == 0 else ""
                print(f"{trial.name} step-{step} {criterion.id}: {verdict['status']} {verdict.get('level', '')}{kept}")
    if not args.execute:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "summary.json").write_text(json.dumps(summarize(rows), indent=2, ensure_ascii=False) + "\n")
    if args.stop:
        print(f"stopped: {args.stop}")
    print(
        f"units by status: {counts}; prompt characters in total: {chars} "
        f"(each unit is sent `samples` times); judge calls made: {args.calls_made}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
