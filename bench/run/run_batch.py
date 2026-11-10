#!/usr/bin/env python3
"""One model, a queue of trials: harnesses x repeats, without anyone in between.

    python3 bench/run/run_batch.py --model qwen3.8-27b              # opencode -> openhands-sdk -> openhands, step 1
    python3 bench/run/run_batch.py --model gemma-4-31b --harnesses opencode --repeats 3 --steps 5 --matrix

Every trial is a separate ``run_trial.py`` process (own proxy, own throwaway
key, own ``run.json``); all of them write under one batch directory. The queue
is: for each harness, ``--repeats`` trials in a row. The model on the server is
never switched here: one batch is one model.

    <out>/batch-<time>-<model>/
      batch.json            one record per trial, with one row per step
      batch-summary.md      overview (a line per trial) and a table per trial (a line per step)
      <harness>-r<N>.log    output of run_trial.py and Harbor
      <job>/...             the usual output of run_trial.py

What makes a trial worth a look is collected per step as *flags*: the trial
did not start, a step ended ``crashed`` / ``infra_failure``, vLLM answered an
HTTP error other than a context overflow (HTTP 400 "maximum context length":
the harness compacts the history and goes on; such answers are only counted),
no tool call was parsed in a step that had turns, the harness spent its turn
limit, the harness's loop detector stopped the agent, a ``completed`` step
ended without the agent's final answer, or the code snapshot of a
step is not a git bundle (``fallback``: only an archive of the working copy;
``missing``: nothing). Flags are always in the
summary. When the batch stops:

  default      after any trial with a flag (review mode: look before anything else runs);
  --matrix     only after a trial that did not start or is ``infra_failure``
               (trial status or a step); ``crashed``, other 400s and steps
               without tool calls do not stop it;
  --keep-going never.

A trial that did not start (upstream, proxy, model guard, image build, network:
no model request was made) is tried again in ``--matrix`` mode: a pause of
``--retry-wait`` seconds (default 120), up to 3 attempts in all, and only then
the batch stops.

A trial that started and ended ``infra_failure`` (the upstream or the server went
away under it) is also run again in ``--matrix`` mode, up to 2 times: the batch
first waits for the server - it polls ``<upstream>/v1/models`` every 30 s for
up to ``--server-wait`` seconds (default 1800; any HTTP answer counts, also
401) - and stops if the server does not come back. The failed attempt stays on
disk and in ``batch.json`` as an attempt; only the last attempt is the trial's
result.

Every attempt keeps its job directory and log (``<harness>-r<N>.log``,
``<harness>-r<N>-attempt2.log``, ...); the attempts are listed in
``batch.json`` (``attempts``).

Exit code: 0 - all trials ran and none has a flag; 1 - stopped or flagged;
3 - the first trial did not start.

The preemption counter of vLLM is read by run_trial.py before and after each
trial (``serving.preemptions`` in run.json); when /metrics cannot be read
through the upstream, the summary prints the command to run on the server.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_HARNESSES = ("opencode", "openhands-sdk", "openhands")
STOP_END_REASONS = ("crashed", "infra_failure")
MATRIX_ATTEMPTS = 3  # --matrix: attempts for a trial that does not start
MATRIX_INFRA_RETRIES = 2  # --matrix: reruns of a trial that started and ended infra_failure
SERVER_POLL_SEC = 30.0
# Passed to run_trial.py as given.
PASS_THROUGH = ("profile", "budget", "agent_timeout", "max_turns", "upstream", "proxy_port", "lock", "task_dir", "served_name")


def log(message: str) -> None:
    print(f"[run_batch {datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def find_run_json(batch_dir: Path, known: set[Path]) -> Path | None:
    """run.json of the job directory that appeared during the last trial."""
    for job in sorted(p for p in batch_dir.iterdir() if p.is_dir() and p not in known):
        found = sorted(job.glob("*/run.json")) or sorted(job.glob("run.json"))
        if found:
            return found[0]
    return None


def failed_criteria(run_json: Path, step: str) -> list[str] | None:
    report = _load(run_json.parent / "steps" / step / "verifier" / "criteria.json")
    if not report:
        return None
    return [c["id"] for c in report.get("criteria", []) if c.get("scored", True) and c.get("status") != "pass"]


def step_row(step: dict[str, Any], window: int | None, run_json: Path | None) -> dict[str, Any]:
    """One step of a trial: the per-step line of the summary table, from run.json."""
    tokens = step.get("tokens") or {}
    peak = step.get("peak_context_tokens")
    cats = step.get("turn_categories") or {}
    http = cats.get("http_errors") or {}
    return {
        "step": step.get("step"),
        "end_reason": step.get("end_reason"),
        "end_reason_notes": step.get("end_reason_notes"),
        "harness_signal": step.get("harness_signal"),
        "last_message_is_question": step.get("last_message_is_question"),
        "reward": (step.get("reward") or {}).get("reward"),
        "no_final_answer": step.get("no_final_answer"),
        "turn_limit_hit": step.get("turn_limit_hit"),
        "iterations": step.get("iterations"),
        "template_rejected_harness_message": step.get("template_rejected_harness_message"),
        "loop_detected": (step.get("loop_detected") or {}).get("detected"),
        "loop_detected_message": (step.get("loop_detected") or {}).get("message"),
        "snapshot": (step.get("snapshot") or {}).get("status"),
        "snapshot_error": (step.get("snapshot") or {}).get("error"),
        "failed_criteria": failed_criteria(run_json, step["step"]) if run_json and step.get("step") else None,
        "turns": step.get("turns"),
        "auxiliary_requests": step.get("auxiliary_requests"),
        "completion_tokens": tokens.get("completion"),
        "reasoning_tokens": tokens.get("reasoning"),
        "peak_context_tokens": peak,
        "peak_context_share": round(peak / window, 3) if peak and window else None,
        "length_finishes": step.get("length_finishes"),
        "output_limit_hits": (cats.get("output_limit_hits") or {}).get("count"),
        "invalid_tool_names": (cats.get("invalid_tool_names") or {}).get("count"),
        "longest_response": cats.get("longest_response"),
        "history_shrinks": step.get("history_shrinks"),
        "condensations": (cats.get("condensations") or {}).get("answered"),
        "condenser": step.get("condenser"),
        "upstream_errors": step.get("upstream_errors"),
        "upstream_waits": step.get("upstream_waits"),
        "upstream_wait_seconds": step.get("upstream_wait_seconds"),
        "http_errors": step.get("http_errors"),
        "http_errors_context_overflow": http.get("context_overflow"),
        # Without the traffic breakdown every HTTP error counts as "other" (fail closed).
        "http_errors_other": http.get("other", step.get("http_errors")),
        "http_errors_other_examples": http.get("other_examples"),
        "client_aborted": step.get("client_aborted"),
        "tool_calls": cats.get("tool_calls"),
        "tool_calls_unparsed_arguments": cats.get("tool_calls_unparsed_arguments"),
        "wall_time_sec": step.get("wall_time_sec"),
        "sec_to_last_successful_tool_call": (cats.get("last_successful_tool_call") or {}).get("sec_from_step_start"),
        "tool_results": cats.get("tool_results"),
        "turn_category_token_shares": {
            name: data.get("token_share") for name, data in (cats.get("categories") or {}).items()
        },
    }


def trial_row(harness: str, exit_code: int, run_json: Path | None, repeat: int = 1) -> dict[str, Any]:
    """A trial: trial-level facts and one row per step, from run.json."""
    row: dict[str, Any] = {
        "harness": harness, "repeat": repeat, "exit_code": exit_code,
        "run_json": str(run_json) if run_json else None, "steps": [],
    }
    run = _load(run_json) if run_json else None
    if not run:
        row["status"] = "no run.json"
        return row
    row["status"], row["status_reason"] = run.get("status"), run.get("status_reason")
    row["warnings"] = run.get("warnings")
    row["reward"] = ((run.get("harbor") or {}).get("reward") or {}).get("reward")
    preemptions = (run.get("serving") or {}).get("preemptions") or {}
    row["preemptions"] = {
        "during_trial": preemptions.get("during_trial"),
        "after": (preemptions.get("after") or {}).get("value"),
        "command": (preemptions.get("after") or {}).get("command"),
    }
    libraries = (run.get("environment") or {}).get("library_versions")
    row["library_versions_diverged"] = libraries.get("diverged") if libraries else None
    row["library_versions_differ"] = (
        {name: pair for snapshot in libraries["snapshots"] for name, pair in snapshot["differ"].items()}
        if libraries
        else None
    )
    window = (run.get("serving") or {}).get("max_model_len")
    row["steps"] = [step_row(step, window, run_json) for step in run.get("steps") or []]
    if any(step["end_reason"] == "timeout" for step in row["steps"]):
        # Harbor's mean leaves out a step without a verdict; a step over the TTL counts as 0.
        rewards = [step["reward"] for step in row["steps"]]
        if rewards and all(reward is not None for reward in rewards):
            row["reward"] = sum(rewards) / len(rewards)
    return row


def not_started(row: dict[str, Any]) -> bool:
    """The trial made no model request: nothing of the agent's work is lost by running it again."""
    if row["exit_code"] == 3 or row.get("status") == "no run.json":
        return True
    return row.get("status") == "infra_failure" and not any(s.get("turns") for s in row.get("steps") or [])


def infra_failed(row: dict[str, Any]) -> bool:
    """The trial started and is an infrastructure failure (trial status or a step)."""
    return row.get("status") == "infra_failure" or any(s.get("end_reason") == "infra_failure" for s in row.get("steps") or [])


def next_action(row: dict[str, Any], mode: str, unstarted_attempts: int, infra_retries: int) -> str:
    """What the batch does after an attempt: `done`, `retry_unstarted` or `retry_infra`.

    Only in matrix mode anything is run again: an unstarted trial up to
    MATRIX_ATTEMPTS attempts in all, a started one that ended infra_failure up
    to MATRIX_INFRA_RETRIES more times.
    """
    if mode != "matrix":
        return "done"
    if not_started(row):
        return "retry_unstarted" if unstarted_attempts < MATRIX_ATTEMPTS else "done"
    if infra_failed(row):
        return "retry_infra" if infra_retries < MATRIX_INFRA_RETRIES else "done"
    return "done"


def server_answers(upstream: str, timeout: float = 10.0) -> bool:
    """vLLM answers on /v1/models through the upstream (any HTTP status: without a key it is 401)."""
    try:
        with urllib.request.urlopen(f"{upstream.rstrip('/')}/v1/models", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def wait_for_server(upstream: str, max_wait: float, interval: float = SERVER_POLL_SEC) -> float | None:
    """Poll the server until it answers; seconds waited, or None when it did not come back in time."""
    started = time.monotonic()
    while True:
        if server_answers(upstream):
            return round(time.monotonic() - started, 1)
        if time.monotonic() - started >= max_wait:
            return None
        time.sleep(min(interval, max(max_wait - (time.monotonic() - started), 0.1)))


def step_flags(step: dict[str, Any]) -> list[str]:
    """What makes one step worth a look."""
    flags = []
    if step.get("end_reason") in ("crashed", "infra_failure"):
        flags.append(f"end_reason {step['end_reason']}")
    if step.get("http_errors_other"):
        examples = "; ".join(f"{e['status']} {e['message'][:120]}" for e in step.get("http_errors_other_examples") or [])
        flags.append(f"{step['http_errors_other']} HTTP error response(s) from vLLM other than context overflow" + (f": {examples}" if examples else ""))
    if step.get("end_reason") and not step.get("tool_calls"):
        flags.append("no tool call was parsed")
    if step.get("turn_limit_hit"):
        flags.append(f"turn limit hit ({step.get('iterations')} iterations)"
                     + ("; the chat template rejected the harness's final message" if step.get("template_rejected_harness_message") else ""))
    elif step.get("loop_detected"):
        flags.append("stopped by the harness's loop detector" + (f" ({step['loop_detected_message']})" if step.get("loop_detected_message") else ""))
    elif step.get("no_final_answer") and step.get("end_reason") == "completed":
        flags.append("ended without a final answer")
    if step.get("snapshot") in ("fallback", "missing"):
        flags.append(f"code snapshot {step['snapshot']}" + (f" ({step['snapshot_error']})" if step.get("snapshot_error") else ""))
    return flags


def trial_flags(row: dict[str, Any]) -> list[str]:
    """Flags of a trial: its own and those of every step (prefixed with the step name)."""
    flags = []
    if row["exit_code"] == 3 or row.get("status") == "no run.json":
        flags.append("the trial did not start")
    elif row.get("status") == "infra_failure" and not any(s.get("end_reason") == "infra_failure" for s in row.get("steps") or []):
        flags.append(f"infra_failure: {row.get('status_reason')}")
    for step in row.get("steps") or []:
        flags += [f"{step.get('step')}: {flag}" for flag in step_flags(step)]
    return flags


def stop_reasons(row: dict[str, Any], mode: str = "review") -> list[str]:
    """Flags that stop the batch after this trial.

    `review`: every flag. `matrix`: only a trial that did not start or an
    infrastructure failure (trial status or a step's end reason).
    """
    flags = trial_flags(row)
    if mode == "matrix":
        return [f for f in flags if "did not start" in f or "infra_failure" in f]
    return flags


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False) if value else "—"
    return str(value)


STEP_COLUMNS: list[tuple[str, Any]] = [
    ("step", lambda s: s.get("step")),
    ("end_reason", lambda s: s.get("end_reason")),
    ("signal", lambda s: (s.get("harness_signal") or {}).get("signal")),
    ("reward", lambda s: s.get("reward")),
    ("snapshot", lambda s: s.get("snapshot")),
    ("no final answer / turn limit hit / loop detected", lambda s: f"{_cell(s.get('no_final_answer'))} / {_cell(s.get('turn_limit_hit'))} / {_cell(s.get('loop_detected'))}"),
    ("failed criteria", lambda s: ", ".join(s["failed_criteria"]) if s.get("failed_criteria") else None),
    ("turns / aux", lambda s: f"{_cell(s.get('turns'))} / {_cell(s.get('auxiliary_requests'))}"),
    ("completion / reasoning tokens", lambda s: f"{_cell(s.get('completion_tokens'))} / {_cell(s.get('reasoning_tokens'))}"),
    ("peak context, %", lambda s: round(s["peak_context_share"] * 100, 1) if s.get("peak_context_share") else None),
    ("length / at output limit", lambda s: f"{_cell(s.get('length_finishes'))} / {_cell(s.get('output_limit_hits'))}"),
    ("longest response (tokens, s)", lambda s: (lambda x: f"{x['completion_tokens']}, {x['model_sec']}" if x else None)(s.get("longest_response"))),
    ("shrinks / sdk summaries", lambda s: f"{_cell(s.get('history_shrinks'))} / {_cell(s.get('condensations'))}"),
    ("upstream / 400 overflow / other HTTP", lambda s: f"{_cell(s.get('upstream_errors'))} / {_cell(s.get('http_errors_context_overflow'))} / {_cell(s.get('http_errors_other'))}"),
    ("upstream waits (count, s)", lambda s: f"{_cell(s.get('upstream_waits'))}, {_cell(s.get('upstream_wait_seconds'))}"),
    ("client_aborted", lambda s: s.get("client_aborted")),
    ("tool calls / unparsed / bad names", lambda s: f"{_cell(s.get('tool_calls'))} / {_cell(s.get('tool_calls_unparsed_arguments'))} / {_cell(s.get('invalid_tool_names'))}"),
    ("tool results / failed", lambda s: (lambda x: f"{x['returned']} / {x['failed']}" if x else None)(s.get("tool_results"))),
    ("step time / to last ok call, s", lambda s: f"{_cell(s.get('wall_time_sec'))} / {_cell(s.get('sec_to_last_successful_tool_call'))}"),
    ("turn categories (token share)", lambda s: ", ".join(f"{k} {v:.0%}" for k, v in (s.get("turn_category_token_shares") or {}).items() if v) or None),
]


def _sum(steps: list[dict[str, Any]], key: str) -> float | None:
    values = [s[key] for s in steps if isinstance(s.get(key), (int, float))]
    return sum(values) if values else None


def summary_markdown(model: str, rows: list[dict[str, Any]]) -> str:
    def md(value: Any) -> str:
        return _cell(value).replace("|", "\\|")

    lines = [f"# Batch: {model}", "", "## Trials", ""]
    lines.append("| trial | status | reward (trial) | rewards by step | end reasons by step | time, s | completion tokens | library versions differ | preemptions (counter after) | flags | stopped the batch |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        steps = row.get("steps") or []
        differ = row.get("library_versions_differ") if row.get("library_versions_diverged") else ("no" if row.get("library_versions_diverged") is False else None)
        preemptions = row.get("preemptions") or {}
        cells = [
            f"{row['harness']} #{row.get('repeat', 1)}" + (f" (attempt {len(row['attempts'])})" if len(row.get("attempts") or []) > 1 else ""),
            row.get("status"),
            row.get("reward"),
            " ".join(_cell(s.get("reward")) for s in steps) or None,
            " ".join(_cell(s.get("end_reason")) for s in steps) or None,
            _sum(steps, "wall_time_sec"),
            _sum(steps, "completion_tokens"),
            differ,
            f"{_cell(preemptions.get('during_trial'))} ({_cell(preemptions.get('after'))})",
            "; ".join(row.get("flags") or []) or None,
            "; ".join(row.get("stop_reasons") or []) or None,
        ]
        lines.append("| " + " | ".join(md(c) for c in cells) + " |")
    for row in rows:
        lines += ["", f"## {row['harness']} #{row.get('repeat', 1)}", ""]
        if not row.get("steps"):
            lines.append(f"No steps: {row.get('status')} {row.get('status_reason') or ''}".rstrip())
            continue
        condensers = {json.dumps(s["condenser"], sort_keys=True) for s in row["steps"] if s.get("condenser")}
        for condenser in sorted(condensers):
            info = json.loads(condenser)
            lines += [f"Condenser: {info.get('kind')} max_size={info.get('max_size')} keep_first={info.get('keep_first')}", ""]
        lines.append("| " + " | ".join(title for title, _ in STEP_COLUMNS) + " |")
        lines.append("|" + "---|" * len(STEP_COLUMNS))
        for step in row["steps"]:
            lines.append("| " + " | ".join(md(getter(step)) for _, getter in STEP_COLUMNS) + " |")
    commands = {(r.get("preemptions") or {}).get("command") for r in rows} - {None}
    if commands:
        lines += ["", "Preemptions were not read through the upstream. On the server, after the batch:", ""]
        lines += [f"    {command}" for command in sorted(commands)]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="key in serving/models.lock.json")
    parser.add_argument("--harnesses", nargs="+", default=list(DEFAULT_HARNESSES),
                        choices=["opencode", "openhands-sdk", "openhands", "oracle"])
    parser.add_argument("--repeats", type=int, default=1, help="trials per harness, one after another (default 1)")
    parser.add_argument("--steps", type=int, default=1, help="run only the first N steps (default 1)")
    parser.add_argument("--out", default=os.environ.get("BENCH_RUNS_DIR", str(Path.home() / "bench-runs")))
    parser.add_argument("--matrix", action="store_true",
                        help="stop only after a trial that did not start or is infra_failure")
    parser.add_argument("--keep-going", action="store_true", help="never stop the batch")
    parser.add_argument("--retry-wait", type=float, default=120.0,
                        help="--matrix: seconds to wait before a trial that did not start is tried again")
    parser.add_argument("--server-wait", type=float, default=1800.0,
                        help="--matrix: seconds to wait for the server after an infra_failure before the trial is run again")
    parser.add_argument("--no-caffeinate", action="store_true")
    for name in PASS_THROUGH:
        parser.add_argument("--" + name.replace("_", "-"), help="passed to run_trial.py")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    mode = "matrix" if args.matrix else "review"

    upstream = (args.upstream or json.loads((HERE / "harnesses.json").read_text())["upstream"]).rstrip("/")
    batch_dir = Path(args.out).expanduser().resolve() / f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{args.model}"
    batch_dir.mkdir(parents=True)
    queue = [(harness, repeat) for harness in args.harnesses for repeat in range(1, args.repeats + 1)]
    log(f"batch directory: {batch_dir}; {len(queue)} trial(s), steps 1-{args.steps}, stop mode: "
        + ("never" if args.keep_going else mode))
    rows: list[dict[str, Any]] = []
    stopped: str | None = None
    for position, (harness, repeat) in enumerate(queue):
        label = f"{harness} #{repeat}"
        command = [sys.executable, str(HERE / "run_trial.py"), "--model", args.model, "--harness", harness,
                   "--steps", str(args.steps), "--out", str(batch_dir)]
        for name in PASS_THROUGH:
            if getattr(args, name) is not None:
                command += ["--" + name.replace("_", "-"), str(getattr(args, name))]
        if args.no_caffeinate:
            command.append("--no-caffeinate")
        attempts: list[dict[str, Any]] = []
        unstarted_attempts = infra_retries = 0
        server_lost = False
        while True:
            attempt = len(attempts) + 1
            known = {p for p in batch_dir.iterdir() if p.is_dir()}
            trial_log = batch_dir / (f"{harness}-r{repeat}.log" if attempt == 1 else f"{harness}-r{repeat}-attempt{attempt}.log")
            log(f"{label}: starting" + (f" (attempt {attempt})" if attempt > 1 else "") + f"; log: {trial_log}")
            with trial_log.open("w") as output:
                code = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=False).returncode
            row = trial_row(harness, code, find_run_json(batch_dir, known), repeat)
            unstarted_attempts += not_started(row)
            action = next_action(row, "review" if args.keep_going and not args.matrix else mode, unstarted_attempts, infra_retries)
            attempts.append({"attempt": attempt, "exit_code": code, "status": row.get("status"),
                             "status_reason": row.get("status_reason"), "run_json": row.get("run_json"), "log": str(trial_log),
                             "end_reasons": [s.get("end_reason") for s in row.get("steps") or []], "then": action})
            if action == "retry_unstarted":
                log(f"{label}: did not start (exit {code}: {row.get('status_reason')}); next attempt in {args.retry_wait:g} s")
                time.sleep(args.retry_wait)
            elif action == "retry_infra":
                infra_retries += 1
                log(f"{label}: infra_failure ({row.get('status_reason')}); waiting for the server at {upstream} "
                    f"(up to {args.server_wait:g} s), then rerun {infra_retries} of {MATRIX_INFRA_RETRIES}")
                waited = wait_for_server(upstream, args.server_wait)
                attempts[-1]["server_back_after_sec"] = waited
                if waited is None:
                    server_lost = True
                    log(f"{label}: the server did not come back in {args.server_wait:g} s")
                    break
                log(f"{label}: the server answers again after {waited:g} s")
            else:
                break
        row["attempts"] = attempts
        row["server_lost"] = server_lost
        row["flags"] = trial_flags(row)
        row["stop_reasons"] = [] if args.keep_going else stop_reasons(row, mode)
        rows.append(row)
        (batch_dir / "batch.json").write_text(json.dumps(
            {"model": args.model, "steps": args.steps, "repeats": args.repeats, "stop_mode": "never" if args.keep_going else mode,
             "trials": rows}, indent=2, ensure_ascii=False) + "\n")
        (batch_dir / "batch-summary.md").write_text(summary_markdown(args.model, rows))
        steps = row.get("steps") or []
        log(f"{label}: exit {code}, {row.get('status')}, steps: "
            + (", ".join(f"{s.get('step')} {s.get('end_reason')} {_cell(s.get('reward'))}" for s in steps) or "none")
            + f"; preemptions during trial {(row.get('preemptions') or {}).get('during_trial')}"
            + (f"; attempts: {len(attempts)}" if len(attempts) > 1 else "")
            + (f"; flags: {'; '.join(row['flags'])}" if row["flags"] else ""))
        if row["stop_reasons"]:
            left = [f"{h} #{r}" for h, r in queue[position + 1 :]]
            stopped = f"stopped after {label}: {'; '.join(row['stop_reasons'])}" + (f"; not run: {', '.join(left)}" if left else "")
            log(stopped)
            break
    print()
    print(summary_markdown(args.model, rows))
    log(f"summary: {batch_dir / 'batch-summary.md'}")
    if rows and rows[0]["exit_code"] == 3:
        return 3
    return 1 if stopped or any(row["flags"] for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
