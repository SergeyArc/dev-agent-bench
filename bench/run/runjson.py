"""Builds ``run.json`` for one trial from Harbor's output and the proxy state."""

from __future__ import annotations

import hashlib
import json
import re
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import g2
import turncats

PROJECT_ENVIRONMENT = "/app/.venv"
# Harbor's message for a harness process that returned a non-zero code.
_HARNESS_EXIT = re.compile(r"Command failed \(exit (\d+)\)")
# A step that timed out after the proxy waited for the server longer than this is an infrastructure failure.
UPSTREAM_WAIT_INFRA_SEC = 600
IMAGE_UV_PATH = "/usr/local/bin/uv"
END_REASONS = ("completed", "timeout", "budget_exhausted", "crashed", "infra_failure")
# Harbor exception types that are about the infrastructure, not the agent's work.
INFRA_EXCEPTIONS = {
    "AgentSetupTimeoutError",
    "EnvironmentStartTimeoutError",
    "HealthcheckError",
    "VerifierTimeoutError",
}


def sha256_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def sha256_tree(root: Path) -> str:
    """Hash of file paths and contents under ``root`` (stable order)."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def git_state(repo: Path) -> dict[str, Any]:
    def git(*args: str) -> str:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
        return result.stdout.strip()

    return {"commit": git("rev-parse", "HEAD") or None, "dirty": bool(git("status", "--porcelain"))}


def input_hashes(repo: Path, step_names: list[str], bench: Path | None = None) -> dict[str, Any]:
    """Hashes of what the trial ran. `bench`: the task directory, when it is not ``bench/`` (a variant)."""
    bench = bench or repo / "bench"
    return {
        "contract": sha256_file(repo / "docs" / "contract.md"),
        "agents_md": sha256_file(bench / "environment" / "app" / "AGENTS.md"),
        "task_toml": sha256_file(bench / "task.toml"),
        "step_instructions": {
            name: sha256_file(bench / "steps" / name / "instruction.md") for name in step_names
        },
        "environment_context": sha256_tree(bench / "environment"),
    }


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _seconds(timing: dict[str, Any] | None) -> float | None:
    if not timing or not timing.get("started_at") or not timing.get("finished_at"):
        return None
    start = datetime.fromisoformat(timing["started_at"])
    end = datetime.fromisoformat(timing["finished_at"])
    return round((end - start).total_seconds(), 1)


def last_agent_text(trajectory: dict[str, Any] | None) -> tuple[str | None, int]:
    """Text of the last model message in an ATIF trajectory and the agent step count."""
    if not trajectory:
        return None, 0
    agent_steps = [s for s in trajectory.get("steps") or [] if s.get("source") == "agent"]
    for step in reversed(agent_steps):
        # OpenHands ends with a `finish` tool call that carries the model's
        # message; the step's own `message` is then OpenHands' canned phrase.
        for call in step.get("tool_calls") or []:
            text = (call.get("arguments") or {}).get("message")
            if call.get("function_name") == "finish" and isinstance(text, str) and text.strip():
                return text.strip(), len(agent_steps)
        message = step.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip(), len(agent_steps)
    return None, len(agent_steps)


def is_question(text: str | None) -> bool:
    """Heuristic: the message ends with a question mark (markdown and quotes ignored)."""
    if not text:
        return False
    return text.rstrip(" \t\r\n*_`\"'»)”").endswith("?")


def harness_signal(exception: dict[str, Any] | None) -> dict[str, Any] | None:
    """Signal that ended the harness process, from Harbor's "Command failed (exit N)"."""
    match = re.search(r"Command failed \(exit (\d+)\)", str((exception or {}).get("exception_message", "")))
    if not match or int(match.group(1)) <= 128:
        return None
    number = int(match.group(1)) - 128
    try:
        name = signal.Signals(number).name
    except ValueError:
        name = f"signal {number}"
    return {"exit_code": int(match.group(1)), "signal": name}


def upstream_waits_from_traffic(traffic_dir: Path | None, step: str) -> tuple[int, float]:
    """Waits for an unreachable upstream in the step, from the traffic log: their number and seconds.

    A wait that ended without an answer counts too. Proxies before this rule
    did not record such a wait: the whole time of the failed request is taken.
    """
    index = traffic_dir / "index.jsonl" if traffic_dir else None
    if not index or not index.is_file():
        return 0, 0.0
    count, seconds = 0, 0.0
    for line in index.read_text().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("step") != step:
            continue
        waits = entry.get("upstream_waits") or []
        count += len(waits)
        seconds += sum(wait.get("waited_sec") or 0 for wait in waits)
        if entry.get("status") == 502 and entry.get("upstream_error") and not any(wait.get("gave_up") for wait in waits):
            try:
                lost = float(entry.get("elapsed_sec") or 0)
            except (TypeError, ValueError):
                lost = 0.0
            if lost >= 60:
                count += 1
                seconds += lost
    return count, round(seconds, 1)


def outside_step_refusals(
    traffic_dir: Path | None, proxy_state: dict[str, Any], harbor_result: dict[str, Any] | None
) -> dict[str, int]:
    """Generation requests the proxy refused outside any step: `after_agent_timeout` and `other`.

    Harbor stops the agent at the step TTL and the proxy closes the step, but the
    agent's process can still send one more request before it is killed. Such a
    request - after the end of a step that ended with AgentTimeoutError and
    before the next step began - is the agent's, not an infrastructure failure.
    Any refusal that cannot be placed like that counts as `other`.
    """
    total = (proxy_state.get("outside_step") or {}).get("requests") or 0
    index = traffic_dir / "index.jsonl" if traffic_dir else None
    if not total or not index or not index.is_file():
        return {"after_agent_timeout": 0, "other": total}

    def moment(value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    timed_out = {
        s["step_name"]
        for s in (harbor_result or {}).get("step_results") or []
        if (s.get("exception_info") or {}).get("exception_type") == "AgentTimeoutError"
    }
    steps = [(moment(s.get("started_at")), moment(s.get("ended_at")), name) for name, s in (proxy_state.get("steps") or {}).items()]
    excused = 0
    for line in index.read_text().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        at = moment(entry.get("started_at"))
        if entry.get("refused") != "no_active_step" or at is None:
            continue
        before = [s for s in steps if s[0] is not None and s[0] <= at]
        if not before:
            continue
        _, ended, name = max(before, key=lambda s: s[0])
        excused += ended is not None and ended <= at and name in timed_out
    excused = min(excused, total)
    return {"after_agent_timeout": excused, "other": total - excused}


def refused_after_timeout_warning(count: int) -> str:
    return f"{count} generation request(s) refused right after an agent timeout, before the agent was stopped"


def step_end_reason(
    harbor_step: dict[str, Any] | None, proxy_step: dict[str, Any] | None, infra_notes: list[str]
) -> tuple[str, list[str]]:
    """Priority: infra_failure > budget_exhausted > timeout > crashed > completed."""
    notes: list[str] = list(infra_notes)
    if harbor_step is None:
        return "infra_failure", [*notes, "step is missing from Harbor's result"]
    if proxy_step is None:
        return "infra_failure", [*notes, "proxy saw no boundary for this step"]
    # The agent-phase outcome of a step lives in step_results[].exception_info:
    # Harbor keeps the trial-level exception_info empty for an agent timeout.
    exception = (harbor_step.get("exception_info") or {}).get("exception_type")
    if proxy_step["ended_on_upstream_error"]:
        # Errors the harness retried successfully are only counted.
        notes.append("the last model request of the step failed upstream and nothing succeeded after it")
    if exception in INFRA_EXCEPTIONS:
        notes.append(f"Harbor exception {exception}")
    no_verdict = harbor_step.get("verifier_result") is None
    timed_out = exception == "AgentTimeoutError"
    if no_verdict and not timed_out:
        notes.append("verifier produced no result")
    if notes:
        return "infra_failure", notes
    if proxy_step["refused_budget"]:
        return "budget_exhausted", []
    if timed_out:
        waited = proxy_step.get("upstream_wait_seconds") or 0
        if waited > UPSTREAM_WAIT_INFRA_SEC:
            # The step ran out of time, and the proxy spent a large part of it waiting for the server.
            return "infra_failure", [
                f"timeout after {waited:g} s of waiting for an unreachable upstream (more than {UPSTREAM_WAIT_INFRA_SEC} s)",
                *(["verifier produced no result"] if no_verdict else []),
            ]
        # The step is failed by the TTL whatever the verifier says, also when it says nothing.
        return "timeout", ["verifier produced no result"] if no_verdict else []
    if exception:
        return "crashed", [f"Harbor exception {exception}"]
    if not proxy_step["turns"]:
        return "crashed", ["agent finished without a single model response"]
    return "completed", []


def snapshot_status(trial_dir: Path | None, name: str) -> dict[str, Any]:
    """What is left of the step's code snapshot (bench-snapshot, collected by Harbor).

    `ok`: a git bundle; `fallback`: git could not make one (the agent broke
    `.git`), an archive of the working copy was kept instead; `missing`:
    neither is there.
    """
    info: dict[str, Any] = {"status": "missing", "error": None, "bundle": False, "archive": False, "hook_status": None}
    if trial_dir is None:
        info["error"] = "no trial directory"
        return info
    root = trial_dir / "steps" / name / "artifacts"
    meta = next(iter(sorted(root.rglob(f"snapshot-{name}.json"))), None) if root.is_dir() else None
    data = _load(meta) if meta else None
    if not isinstance(data, dict):
        info["error"] = "no snapshot metadata among the step's artifacts"
        return info
    info["hook_status"], info["error"] = data.get("status"), data.get("error") or None
    info["bundle"] = any(p.stat().st_size > 0 for p in meta.parent.glob(f"snapshot-{name}.bundle"))
    info["archive"] = any(p.stat().st_size > 0 for p in meta.parent.glob(f"snapshot-{name}.tar.gz"))
    info["status"] = "ok" if info["bundle"] else "fallback" if info["archive"] else "missing"
    if info["status"] == "missing" and not info["error"]:
        info["error"] = "the snapshot file was not collected"
    return info


_LOOP_DETECTED = re.compile(r"AgentStuckInLoopError|stuck\.py:\d+ - (?P<what>[^\n]{0,120}?(?:loop|pattern) detected[^\n]{0,80})")


def loop_detected(trial_dir: Path | None, name: str) -> dict[str, Any]:
    """OpenHands' loop detector ended the step (from the harness log of the step).

    OpenHands 1.6.0 stops a session when the same action and observation repeat
    (`stuck.py: Action, Observation loop detected`, then AgentStuckInLoopError):
    the agent state becomes ERROR and the process exits normally, so the step
    is still `completed`. `detected` is None when the step has no harness log.
    """
    info: dict[str, Any] = {"detected": None, "message": None}
    agent_dir = trial_dir / "steps" / name / "agent" if trial_dir else None
    logs = sorted(agent_dir.glob("*.txt")) if agent_dir and agent_dir.is_dir() else []
    if not logs:
        return info
    info["detected"] = False
    for log in logs:
        try:
            text = log.read_text(errors="replace")
        except OSError:
            continue
        found = None
        for found in _LOOP_DETECTED.finditer(text):
            if found.group("what"):
                break
        if found:
            info["detected"] = True
            info["message"] = (found.group("what") or "AgentStuckInLoopError").strip()[:200]
            break
    return info


def turn_limit_flags(cats: dict[str, Any] | None, proxy: dict[str, Any], max_turns: int | None) -> dict[str, Any]:
    """`no_final_answer` and `turn_limit_hit` of a step.

    no_final_answer: the last turn is not the agent's own final answer (a
    `finish` call or a message without a tool call) - the step was cut short.
    turn_limit_hit: the harness spent its iteration limit. OpenHands counts as
    an iteration every model call, also the ones answered with a context-overflow
    400, and every condensation of the history; so the iterations of a step are
    turns + overflow answers + history shrinks (300 exactly in the steps the
    limit stopped). opencode ends a session at its step limit by asking for a
    final text after its own `assistant` message; when the chat template
    rejects that request (Mistral), it is the last iteration and is counted
    too, and `template_rejected_harness_message` is set. None when there were
    no turns or the limit is unknown.
    """
    last = (cats or {}).get("last_turn")
    turns = proxy.get("turns")
    if not last or not turns:
        return {"no_final_answer": None, "turn_limit_hit": None, "iterations": None, "template_rejected_harness_message": None}
    http = (cats or {}).get("http_errors") or {}
    rejected = http.get("template_rejected") or 0
    iterations = turns + (http.get("context_overflow") or 0) + (proxy.get("history_shrinks") or 0) + rejected
    return {
        "no_final_answer": not last.get("final_answer"),
        "turn_limit_hit": iterations >= max_turns if max_turns else None,
        "iterations": iterations,
        "template_rejected_harness_message": bool(rejected),
    }


def finished_after_overflow(
    reason: str, exception: dict[str, Any] | None, proxy_step: dict[str, Any] | None, cats: dict[str, Any] | None
) -> bool:
    """The harness left with a non-zero exit code although the step was finished.

    opencode records a context-overflow answer of vLLM (HTTP 400 "maximum
    context length") as a session error, compacts the history, goes on to the
    end - and then exits with code 1 because of that recorded error. Such a
    step is `completed`, not `crashed`. All of this must hold: the harness
    process ended by itself with a non-zero code, no signal (Harbor's message
    starts with "Command failed (exit N)"; the exception type varies with what
    Harbor recognizes in the harness output: NonZeroAgentExitCodeError,
    ApiOverloadedError, NetworkConnectionError); the only HTTP
    errors of the step are context overflows; after the last of them the model
    was asked again, and its last answer is a normal final message
    (`finish_reason: stop`, no tool call).
    """
    exited = _HARNESS_EXIT.match(str((exception or {}).get("exception_message", "")))
    if reason != "crashed" or not exited or not 0 < int(exited.group(1)) <= 128:
        return False
    http = (cats or {}).get("http_errors") or {}
    last = (proxy_step or {}).get("last_response") or {}
    overflow_seq = http.get("last_context_overflow_seq")
    return bool(
        http.get("context_overflow")
        and not http.get("other")
        and overflow_seq is not None
        and last.get("seq") is not None
        and last["seq"] > overflow_seq
        and last.get("finish_reason") == "stop"
        and not last.get("tool_calls")
    )


def build_steps(
    trial_dir: Path | None,
    harbor_result: dict[str, Any] | None,
    proxy_state: dict[str, Any],
    step_names: list[str],
    traffic_dir: Path,
    infra_notes: list[str],
    max_turns: int | None = None,
) -> list[dict[str, Any]]:
    harbor_steps = {s["step_name"]: s for s in (harbor_result or {}).get("step_results") or []}
    steps: list[dict[str, Any]] = []
    for name in step_names:
        harbor_step = harbor_steps.get(name)
        proxy_step = proxy_state.get("steps", {}).get(name)
        if proxy_step:
            # Older proxies did not count the waits that ended without an answer.
            waits, waited = upstream_waits_from_traffic(traffic_dir, name)
            if waited > (proxy_step.get("upstream_wait_seconds") or 0):
                proxy_step = {**proxy_step, "upstream_waits": waits, "upstream_wait_seconds": waited}
        reason, notes = step_end_reason(harbor_step, proxy_step, infra_notes)
        trajectory_path = trial_dir / "steps" / name / "agent" / "trajectory.json" if trial_dir else None
        trajectory = _load(trajectory_path) if trajectory_path else None
        text, atif_agent_steps = last_agent_text(trajectory)
        source = "atif"
        if text is None and proxy_step and proxy_step.get("last_response"):
            text = proxy_step["last_response"]["content_tail"] or None
            source = "proxy"
        verifier_rewards = ((harbor_step or {}).get("verifier_result") or {}).get("rewards")
        # A step over the TTL is failed: reward 0; what the verifier measured is kept apart.
        rewards = {"reward": 0.0} if reason == "timeout" else verifier_rewards
        proxy = proxy_step or {}
        exception = (harbor_step or {}).get("exception_info")
        cats = turncats.summarize(traffic_dir, name, proxy.get("started_at"))
        exit_after_overflow = finished_after_overflow(reason, exception, proxy_step, cats)
        # Hook use and bypass detectors of the G2 ablation variant; None for any other task.
        ablation = g2.step_report(trial_dir, name, traffic_dir)
        if exit_after_overflow:
            reason = "completed"
            notes = [
                "harness exited with a non-zero code after a context overflow it recovered from: "
                "the step went on and ended with a normal final answer"
            ]
        steps.append(
            {
                "step": name,
                "end_reason": reason,
                "end_reason_notes": notes,
                "last_message_is_question": is_question(text),
                "last_message_tail": text[-300:] if text else None,
                "last_message_source": source if text else None,
                "tokens": {
                    "prompt": proxy.get("prompt_tokens"),
                    "completion": proxy.get("completion_tokens"),
                    "reasoning": proxy.get("reasoning_tokens") if proxy.get("reasoning_tokens_reported") else None,
                    "completion_estimated_part": proxy.get("estimated_completion_tokens"),
                    "budget": proxy.get("budget"),
                },
                "turns": proxy.get("turns"),
                "requests": proxy.get("requests"),
                "auxiliary_requests": proxy.get("auxiliary_requests"),
                "auxiliary_completion_tokens": proxy.get("auxiliary_completion_tokens"),
                "length_finishes": proxy.get("length_finishes"),
                # The step ended normally, but its last answer was cut by the output limit.
                "last_response_length": reason == "completed"
                and ((proxy.get("last_response") or {}).get("finish_reason") == "length"),
                "max_response_completion_tokens": proxy.get("max_response_completion_tokens"),
                "history_shrinks": proxy.get("history_shrinks"),
                "max_inflight": proxy.get("max_inflight"),
                "rejected_paths": proxy.get("rejected_paths"),
                "rejected_path_list": proxy.get("rejected_path_list"),
                "usage_estimated": proxy.get("usage_estimated"),
                "client_aborted": proxy.get("client_aborted"),
                "client_aborted_completion_tokens": proxy.get("client_aborted_completion_tokens"),
                "client_timeouts_seen_sec": proxy.get("client_timeouts_seen"),
                # The harness process was ended by a signal (143 = SIGTERM, 137 = SIGKILL).
                "harness_signal": harness_signal(exception),
                "proxy_stripped": proxy.get("stripped_fields"),
                "upstream_errors": proxy.get("upstream_errors"),
                "upstream_retries": proxy.get("upstream_retries"),
                # The proxy waited for an unreachable upstream and sent the request again.
                "upstream_waits": proxy.get("upstream_waits"),
                "upstream_wait_seconds": proxy.get("upstream_wait_seconds"),
                "atif_agent_steps": atif_agent_steps,
                "wall_time_sec": _seconds((harbor_step or {}).get("agent_execution")),
                "verifier_time_sec": _seconds((harbor_step or {}).get("verifier")),
                "peak_context_tokens": proxy.get("peak_context_tokens"),
                "sampling_fields_sent": proxy.get("sampling_fields_sent"),
                "sampling_violations": proxy.get("sampling_violations"),
                "chat_template_kwargs_sent": proxy.get("chat_template_kwargs_sent"),
                "aborted_streams": proxy.get("aborted_streams"),
                "http_errors": proxy.get("http_errors"),
                "reward": rewards,
                "reward_verifier": verifier_rewards,
                # The message can embed the whole instruction; the full text stays in result.json.
                "exception": {
                    "exception_type": exception.get("exception_type"),
                    "exception_message": str(exception.get("exception_message"))[-600:],
                }
                if exception
                else None,
                # What the turns were spent on, from the traffic log (turncats.py).
                # The code of the step: git bundle (ok), archive of the working copy (fallback), nothing (missing).
                "snapshot": snapshot_status(trial_dir, name),
                "turn_categories": cats,
                **turn_limit_flags(cats, proxy, max_turns),
                # The harness's own loop detector stopped the agent (OpenHands' AgentStuckInLoopError).
                "loop_detected": loop_detected(trial_dir, name),
                # The harness process returned a non-zero code, but the step was finished (see finished_after_overflow).
                "nonzero_exit_after_context_overflow": exit_after_overflow,
                # History condenser configured by our SDK launcher (bench_agents.py); the
                # number of condensations is turn_categories.condensations.
                "condenser": _load(trial_dir / "steps" / name / "agent" / "bench-condenser.json") if trial_dir else None,
                "atif_trajectory": str(trajectory_path) if trajectory is not None else None,
                "traffic_log": str(traffic_dir / name) if (traffic_dir / name).is_dir() else None,
                **({"g2": ablation} if ablation is not None else {}),
            }
        )
    return steps


def library_versions(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Harness Python vs /app/.venv for the libraries recorded by the plugin.

    `diverged`: a library installed in both with different versions, at any
    snapshot; None when the harness has no Python environment (opencode).
    """
    if not record:
        return None
    snapshots = []
    diverged: bool | None = None
    harness_root = None
    for snapshot in record.get("snapshots") or []:
        environments = snapshot.get("environments") or {}
        project = (environments.get(PROJECT_ENVIRONMENT) or {}).get("packages")
        harness_root = next((root for root in environments if root != PROJECT_ENVIRONMENT), harness_root)
        harness = (environments.get(harness_root) or {}).get("packages") if harness_root else None
        differ = {
            name: {"harness": harness[name], "project": project.get(name)}
            for name in (harness or {})
            if project and harness[name] and project.get(name) and harness[name] != project.get(name)
        }
        if harness is not None and project is not None:
            diverged = bool(diverged) or bool(differ)
        snapshots.append({"when": snapshot.get("when"), "harness": harness, "project": project, "differ": differ})
    return {
        "packages": record.get("packages"),
        "harness_environment": harness_root,
        "project_environment": PROJECT_ENVIRONMENT,
        "diverged": diverged,
        "snapshots": snapshots,
    }


def uv_warnings(environment: dict[str, Any], image_uv_version: str) -> list[str]:
    """Warn when the agent's shell resolves a uv other than the image's version.

    Another path with the same version is not a warning: the OpenHands installers
    put their own uv into ~/.local/bin, pinned to the image version (bench_agents.py);
    the paths are in `environment.agent_views`.
    """
    warnings: list[str] = []
    views = environment.get("agent_views")
    if not views:
        return ["agent's uv/python were not captured (no agent process seen)"]
    for view in views:
        version = (view.get("uv_version") or "").split()
        number = version[1] if len(version) > 1 else None
        where = f"{view.get('uv')} ({', '.join(view.get('seen_in', []))})"
        if number != image_uv_version:
            warnings.append(f"agent sees uv {number or 'none'} at {where}; the image has {image_uv_version}")
    return warnings


def write_run_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
