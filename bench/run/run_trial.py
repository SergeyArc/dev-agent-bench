#!/usr/bin/env python3
"""One trial: model x harness. Checks the infrastructure, runs Harbor, writes run.json.

    python3 bench/run/run_trial.py --model qwen3.8-27b --harness opencode --steps 1

Order of work:
  1. upstream is up (``/health`` of the vLLM endpoint);
  2. proxy starts and answers;
  3. model guard: ``/v1/models`` through the proxy must list the served model
     expected from ``serving/models.lock.json`` with the profile's context length;
  4. Harbor runs under ``caffeinate`` with the step-boundary plugin;
  5. ``run.json`` is written next to Harbor's trial output.

Exit code: 0 - trial status ``ok``; 2 - ``infra_failure`` (rerun, not in statistics);
3 - the trial did not start (guard failed).

The vLLM API key is read from ``$BENCH_VLLM_API_KEY`` or from the file
``$BENCH_VLLM_API_KEY_FILE`` (default ``~/.config/dev-agent-bench/vllm_api_key``).
It is passed only to the proxy; the container gets a throwaway per-trial key.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import runjson

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEFAULT_KEY_FILE = Path.home() / ".config" / "dev-agent-bench" / "vllm_api_key"
EXIT_INFRA = 2
EXIT_NOT_STARTED = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"[run_trial {datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 10) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except OSError as exc:
        return 0, repr(exc).encode()


PREEMPTIONS_METRIC = "vllm:num_preemptions_total"


def vllm_preemptions(upstream: str) -> dict[str, Any]:
    """Snapshot of the preemption counter from vLLM's /metrics, through the upstream.

    /metrics is asked directly (the proxy serves the two API paths only) and
    without a key. When it cannot be read, `command` is what to run on the server.
    """
    status, body = http_get(f"{upstream}/metrics", timeout=15)
    snapshot: dict[str, Any] = {"at": now_iso(), "http_status": status, "value": None}
    if status == 200:
        values = [
            float(line.rsplit(" ", 1)[1])
            for line in body.decode(errors="replace").splitlines()
            if line.startswith(PREEMPTIONS_METRIC) and len(line.split()) >= 2
        ]
        if values:
            snapshot["value"] = sum(values)
    if snapshot["value"] is None:
        snapshot["command"] = f"curl -s http://127.0.0.1:<vLLM port>/metrics | grep '^{PREEMPTIONS_METRIC}'  # on the vLLM server"
    return snapshot


def read_api_key() -> str:
    key = os.environ.get("BENCH_VLLM_API_KEY", "").strip()
    if key:
        return key
    path = Path(os.environ.get("BENCH_VLLM_API_KEY_FILE", DEFAULT_KEY_FILE)).expanduser()
    if path.is_file():
        return path.read_text().strip()
    raise SystemExit(f"vLLM API key not found: set BENCH_VLLM_API_KEY or create {path}")


def served_name(launch_script: Path) -> str:
    match = re.search(r"--served-model-name\s+(\S+)", launch_script.read_text())
    if not match:
        raise SystemExit(f"--served-model-name not found in {launch_script}")
    return match.group(1)


def step_names(task_toml: Path) -> list[str]:
    return re.findall(r'^\[\[steps\]\]\s*\nname\s*=\s*"([^"]+)"', task_toml.read_text(), flags=re.M)


def prepare_task(dest: Path, n_steps: int | None, source: Path | None = None) -> tuple[Path, list[str]]:
    """Copy ``bench/`` for this trial; optionally keep only the first N steps.

    The copy is what Harbor runs, so the repository task is never edited.
    """
    task_dir = dest / "task" / "bench"
    shutil.copytree(source or REPO / "bench", task_dir, ignore=shutil.ignore_patterns("run", "__pycache__"))
    toml_path = task_dir / "task.toml"
    names = step_names(toml_path)
    if n_steps is not None and n_steps < len(names):
        text = toml_path.read_text()
        blocks = re.split(r"(?m)^(?=\[\[steps\]\]\s*$)", text)
        head, step_blocks = blocks[0], blocks[1:]
        toml_path.write_text(head + "".join(step_blocks[:n_steps]))
        names = names[:n_steps]
        for extra in sorted((task_dir / "steps").glob("step-*")):
            if extra.name not in names:
                shutil.rmtree(extra)
    return task_dir, names


def agent_config(
    harness: str, cfg: dict[str, Any], served: str, max_model_len: int, sampling: dict[str, Any],
    base_url: str, client_key: str, timeout_sec: float,
) -> dict[str, Any]:
    limits = cfg["limits"]
    spec = cfg["harnesses"].get(harness, {})
    if harness == "opencode":
        provider = spec["provider_id"]
        timeout_ms = limits["client_timeout_sec"] * 1000
        model_ref = f"{provider}/{served}"
        config = json.loads(json.dumps(spec["config"]))
        config["model"] = model_ref
        # Title/summary calls go to the same endpoint and model.
        config["small_model"] = model_ref
        config["provider"] = {
            provider: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "bench vLLM",
                # All three opencode timers, in ms: whole request, response headers, gap between chunks.
                "options": {
                    "baseURL": base_url,
                    "apiKey": client_key,
                    "timeout": timeout_ms,
                    "headerTimeout": timeout_ms,
                    "chunkTimeout": timeout_ms,
                },
                "models": {
                    served: {
                        "name": served,
                        "tool_call": True,
                        "reasoning": True,
                        "limit": {"context": max_model_len, "output": limits["max_output_tokens"]},
                        **spec.get("model_options", {}),
                    }
                },
            }
        }
        # Emergency cap only; opencode sends no sampling fields for a custom model.
        config["agent"] = {"build": {"steps": limits["max_turns"]}}
        return {
            "import_path": spec["agent_class"],
            "model_name": model_ref,
            "override_timeout_sec": timeout_sec,
            "override_setup_timeout_sec": limits["agent_setup_timeout_sec"],
            "kwargs": {"version": spec["version"], "opencode_config": config},
        }
    if harness == "openhands":
        # OpenHands always sends temperature and top_p, so they must be the vendor's.
        kwargs: dict[str, Any] = {
            "version": spec["version"],
            "python_version": spec["python_version"],
            "api_base": base_url,
            "max_iterations": limits["max_turns"],
            "temperature": str(sampling["temperature"]),
            "top_p": str(sampling.get("top_p", 1.0)),
            "model_info": {
                "max_input_tokens": max_model_len,
                "max_output_tokens": limits["max_output_tokens"],
            },
        }
        return {
            "import_path": spec["agent_class"],
            "model_name": f"{spec['model_prefix']}{served}",
            "override_timeout_sec": timeout_sec,
            "override_setup_timeout_sec": limits["agent_setup_timeout_sec"],
            "kwargs": kwargs,
            # LLM_TIMEOUT: OpenHands' LLMConfig.timeout, seconds.
            "env": {"LLM_API_KEY": client_key, "LLM_TIMEOUT": str(limits["client_timeout_sec"])},
        }
    if harness == "openhands-sdk":
        # The SDK runner exposes only temperature. The SDK sends no output
        # limit; the proxy adds limits.max_output_tokens.
        return {
            "import_path": spec["agent_class"],
            "model_name": f"{spec['model_prefix']}{served}",
            "override_timeout_sec": timeout_sec,
            "override_setup_timeout_sec": limits["agent_setup_timeout_sec"],
            "kwargs": {
                "version": spec["version"],
                "python_version": spec["python_version"],
                "max_iterations": limits["max_turns"],
                "temperature": float(sampling["temperature"]),
            },
            # BENCH_LLM_TIMEOUT_SEC: read by the launcher in bench_agents.py (LLM.timeout).
            "env": {
                "LLM_API_KEY": client_key,
                "LLM_BASE_URL": base_url,
                "BENCH_LLM_TIMEOUT_SEC": str(limits["client_timeout_sec"]),
            },
        }
    if harness == "oracle":
        # Tests of the runner only: no model calls.
        return {"name": "oracle", "override_timeout_sec": timeout_sec}
    raise SystemExit(f"unknown harness: {harness}")


def wait_port(port: int, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def redact(text: str, secrets_: list[str]) -> str:
    for secret in secrets_:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="key in serving/models.lock.json, e.g. qwen3.8-27b")
    parser.add_argument("--harness", required=True, choices=["openhands-sdk", "openhands", "opencode", "oracle"],
                        help="oracle is for testing the runner only")
    parser.add_argument("--profile", help="serving profile (default from harnesses.json)")
    parser.add_argument("--steps", type=int, help="run only the first N steps")
    parser.add_argument("--out", default=os.environ.get("BENCH_RUNS_DIR", str(Path.home() / "bench-runs")))
    parser.add_argument("--budget", type=int, help="completion tokens per step")
    parser.add_argument("--agent-timeout", type=float, help="seconds per step (emergency cap)")
    parser.add_argument("--max-turns", type=int, help="harness turn limit per step (default: limits.max_turns of harnesses.json)")
    parser.add_argument("--upstream", help="vLLM base URL without /v1 (default: harnesses.json upstream)")
    parser.add_argument("--proxy-port", type=int)
    parser.add_argument("--lock", default=str(REPO / "serving" / "models.lock.json"))
    parser.add_argument("--task-dir", help="task directory to run instead of bench/: a built variant (bench-g2/) or a test task")
    parser.add_argument("--served-name", help="override the served model name (tests only)")
    parser.add_argument("--no-caffeinate", action="store_true")
    args = parser.parse_args()

    cfg = json.loads((HERE / "harnesses.json").read_text())
    if args.max_turns:
        # The limit actually given to the harness; run.json records it in `limits.max_turns`.
        cfg["limits"]["max_turns"] = args.max_turns
    lock = json.loads(Path(args.lock).read_text())
    if args.model not in lock or args.model.startswith("_"):
        raise SystemExit(f"model {args.model!r} is not in {args.lock}")
    model = lock[args.model]
    profile = args.profile or cfg["default_profile"]
    max_model_len = lock["_meta"]["profiles"][profile]["max_model_len"]
    served = args.served_name or served_name(REPO / model["launch_script"])
    sampling = model["override_generation_config"]
    upstream = (args.upstream or cfg["upstream"]).rstrip("/")
    port = args.proxy_port or cfg["proxy_port"]
    budget = args.budget or cfg["limits"]["token_budget_per_step"]
    timeout_sec = args.agent_timeout or cfg["limits"]["agent_timeout_sec"]
    api_key = read_api_key()
    client_key = "trial-" + secrets.token_hex(16)
    control_token = secrets.token_hex(16)

    job_name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    job_dir = Path(args.out).expanduser().resolve() / job_name
    meta_dir = job_dir / "bench-run"
    traffic_dir = meta_dir / "traffic"
    meta_dir.mkdir(parents=True)
    started_at = now_iso()

    run: dict[str, Any] = {
        "schema": "dev-agent-bench/run.json/1",
        "status": "infra_failure",
        "status_reason": None,
        "started_at": started_at,
        "finished_at": None,
        "job_name": job_name,
        "model": {
            "key": args.model,
            "hf_repo": model["hf_repo"],
            "revision_sha": model["revision_sha"],
            "served_name": served,
            "served_models_reported": None,
            "vendor_sampling": sampling,
        },
        "serving": {
            "profile": profile,
            "max_model_len": max_model_len,
            "kv_cache_dtype": lock["_meta"]["profiles"][profile]["kv_cache_dtype"],
            "vllm_image": lock["_meta"]["vllm_image"],
            "vllm_version": lock["_meta"]["vllm_version"],
        },
        "harness": {"name": args.harness, "version_pinned": cfg["harnesses"].get(args.harness, {}).get("version")},
        "harbor_version": cfg["harbor_version"],
        "limits": {**cfg["limits"], "token_budget_per_step": budget, "agent_timeout_sec": timeout_sec},
        "repo": runjson.git_state(REPO),
        "warnings": [],
        "steps": [],
    }
    run_json_path = job_dir / "run.json"

    def finish(status: str, reason: str | None, code: int) -> int:
        run["status"], run["status_reason"], run["finished_at"] = status, reason, now_iso()
        runjson.write_run_json(run_json_path, run)
        log(f"status={status}" + (f" ({reason})" if reason else "") + f"; run.json: {run_json_path}")
        return code

    # 1. Upstream.
    status, body = http_get(f"{upstream}/health")
    if status != 200:
        return finish("infra_failure", f"upstream/vLLM health check failed at {upstream}: {status} {body[:200]!r}", EXIT_NOT_STARTED)
    log(f"upstream {upstream} is healthy")
    preemptions_before = vllm_preemptions(upstream)

    # 2. Proxy.
    proxy_env = {
        **os.environ,
        "BENCH_PROXY_UPSTREAM_KEY": api_key,
        "BENCH_PROXY_CLIENT_KEY": client_key,
        "BENCH_PROXY_CONTROL_TOKEN": control_token,
        "BENCH_PROXY_EXPECTED_SAMPLING": json.dumps(sampling),
    }
    proxy_log = (meta_dir / "proxy.log").open("w")
    proxy = subprocess.Popen(
        [sys.executable, str(HERE / "proxy.py"), "--port", str(port), "--upstream", upstream,
         "--traffic-dir", str(traffic_dir), "--budget", str(budget),
         "--inject-max-tokens", str(cfg["limits"]["max_output_tokens"])],
        env=proxy_env, stdout=proxy_log, stderr=subprocess.STDOUT,
    )
    proxy_url = f"http://127.0.0.1:{port}"
    control_headers = {"X-Bench-Control": control_token}
    try:
        if not wait_port(port) or proxy.poll() is not None:
            return finish("infra_failure", f"proxy did not start on port {port} (see {meta_dir / 'proxy.log'})", EXIT_NOT_STARTED)

        # 3. Model guard, through the proxy.
        status, body = http_get(f"{proxy_url}/v1/models", {"Authorization": f"Bearer {client_key}"})
        if status != 200:
            return finish("infra_failure", f"/v1/models through the proxy: {status} {body[:200]!r}", EXIT_NOT_STARTED)
        models = json.loads(body).get("data", [])
        run["model"]["served_models_reported"] = [
            {"id": m.get("id"), "max_model_len": m.get("max_model_len")} for m in models
        ]
        match = next((m for m in models if m.get("id") == served), None)
        if match is None:
            return finish("infra_failure", f"model guard: expected {served!r}, served {[m.get('id') for m in models]}", EXIT_NOT_STARTED)
        if match.get("max_model_len") not in (None, max_model_len):
            return finish(
                "infra_failure",
                f"model guard: profile {profile} expects max_model_len {max_model_len}, served {match.get('max_model_len')}",
                EXIT_NOT_STARTED,
            )
        log(f"model guard passed: {served}, max_model_len={match.get('max_model_len')}")

        # 4. Harbor.
        source = Path(args.task_dir).expanduser().resolve() if args.task_dir else None
        task_dir, names = prepare_task(meta_dir, args.steps, source)
        # The hashes describe the task that runs: `--task-dir` (a built variant) or bench/.
        run["hashes"] = runjson.input_hashes(REPO, names, source)
        variant = runjson._load(task_dir / "variant.json")
        if variant:
            run["task_variant"] = variant
        base_url = f"http://{cfg['container_host']}:{port}/v1"
        agent = agent_config(args.harness, cfg, served, max_model_len, sampling, base_url, client_key, timeout_sec)
        job_config = {
            "job_name": job_name,
            "jobs_dir": str(job_dir.parent),
            "n_attempts": 1,
            "n_concurrent_trials": 1,
            "retry": {"max_retries": 0},
            "tasks": [{"path": str(task_dir)}],
            "agents": [agent],
        }
        config_path = meta_dir / "harbor-job.json"
        config_path.write_text(json.dumps(job_config, indent=2))
        command = [
            "uvx", "--from", f"harbor=={cfg['harbor_version']}", "harbor", "run",
            "-c", str(config_path), "--plugin", "bench_harbor_plugin:StepBoundaryPlugin", "--yes",
        ]
        if not args.no_caffeinate and shutil.which("caffeinate"):
            command = ["caffeinate", "-dims", *command]
        harbor_env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(filter(None, [str(HERE), os.environ.get("PYTHONPATH", "")])),
            "BENCH_PROXY_CONTROL_URL": proxy_url,
            "BENCH_PROXY_CONTROL_TOKEN": control_token,
            "BENCH_RUN_META_DIR": str(meta_dir),
            # The pre-start probe: vLLM through the upstream, as seen from the container.
            "BENCH_UPSTREAM_PROBE_URL": f"http://{cfg['container_host']}:{urlsplit(upstream).port or 80}/v1/models",
            "BENCH_PROBE_KEY": client_key,
            # Pins the uv that Harbor's OpenHands installers download (bench_agents.py).
            "BENCH_UV_VERSION": cfg["image_uv_version"],
        }
        log("starting Harbor: " + " ".join(command))
        harbor = subprocess.run(command, env=harbor_env, check=False)
        log(f"Harbor exited with code {harbor.returncode}")

        # 5. run.json.
        _, state_raw = http_get(f"{proxy_url}/_control/state", control_headers)
        try:
            proxy_state = json.loads(state_raw)
        except json.JSONDecodeError:
            proxy_state = json.loads((traffic_dir / "state.json").read_text())
        tunnel_after, _ = http_get(f"{upstream}/health")
        preemptions_after = vllm_preemptions(upstream)
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()
        proxy_log.close()

    # The throwaway key is dead once the proxy stops; still keep it out of the saved files.
    # Harbor echoes the agent config into its own configs, results and logs.
    for path in job_dir.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".log", ".txt"} and traffic_dir not in path.parents:
            text = path.read_text(errors="replace")
            if any(secret in text for secret in (client_key, control_token, api_key)):
                path.write_text(redact(text, [client_key, control_token, api_key]))

    trial_dirs = [p.parent for p in job_dir.glob("*/result.json")]
    trial_dir = trial_dirs[0] if len(trial_dirs) == 1 else None
    harbor_result = json.loads((trial_dir / "result.json").read_text()) if trial_dir else None
    infra_notes: list[str] = []
    if harbor.returncode != 0:
        infra_notes.append(f"Harbor exit code {harbor.returncode}")
    if trial_dir is None:
        infra_notes.append(f"expected exactly one trial directory, found {len(trial_dirs)}")
    if tunnel_after != 200:
        # Not a failure by itself: a step is infra_failure only if it stopped because of it.
        run["warnings"].append("upstream/vLLM health check failed after the trial")
    refusals = runjson.outside_step_refusals(traffic_dir, proxy_state, harbor_result)
    run["outside_step_refusals"] = refusals
    if refusals["other"]:
        infra_notes.append(f"{refusals['other']} generation request(s) outside any step were refused")
    if refusals["after_agent_timeout"]:
        # Not a failure: the agent was still sending when Harbor stopped it at the step TTL.
        run["warnings"].append(runjson.refused_after_timeout_warning(refusals["after_agent_timeout"]))
    trial_exception = (harbor_result or {}).get("exception_info")
    if trial_exception and trial_exception.get("exception_type") != "AgentTimeoutError":
        infra_notes.append(f"trial-level Harbor exception {trial_exception.get('exception_type')}")

    run["steps"] = runjson.build_steps(trial_dir, harbor_result, proxy_state, names, traffic_dir, infra_notes,
                                       cfg["limits"]["max_turns"])
    run["harness"]["version_reported"] = ((harbor_result or {}).get("agent_info") or {}).get("version")
    run["harness"]["harbor_agent_config"] = json.loads(redact(json.dumps(agent), [client_key]))
    probe = runjson._load(meta_dir / "upstream_probe.json")
    run["upstream_probe"] = probe
    if probe is None and args.harness != "oracle":
        infra_notes.append("the pre-start upstream probe did not run")
    elif probe is not None and not probe.get("passed"):
        run["steps"] = runjson.build_steps(trial_dir, harbor_result, proxy_state, names, traffic_dir, infra_notes,
                                           cfg["limits"]["max_turns"])
        return finish("infra_failure", f"vLLM reachable from the task container around the proxy: {probe['checks']}", EXIT_NOT_STARTED)
    before, after = preemptions_before["value"], preemptions_after["value"]
    run["serving"]["preemptions"] = {
        "metric": PREEMPTIONS_METRIC,
        "before": preemptions_before,
        "after": preemptions_after,
        # Counter of the whole server: includes anything else it served meanwhile.
        "during_trial": after - before if before is not None and after is not None else None,
    }
    if before is None or after is None:
        run["warnings"].append("vLLM preemptions were not read from /metrics; see serving.preemptions.*.command")
    elif after != before:
        run["warnings"].append(f"vLLM preempted {after - before:g} request(s) during the trial")
    environment = runjson._load(meta_dir / "environment.json") or {}
    run["environment"] = environment
    libraries = runjson.library_versions(runjson._load(meta_dir / "library_versions.json"))
    run["environment"]["library_versions"] = libraries
    if libraries and libraries["diverged"]:
        differ = {name: pair for snapshot in libraries["snapshots"] for name, pair in snapshot["differ"].items()}
        run["warnings"].append(
            f"library versions differ between {libraries['harness_environment']} and {libraries['project_environment']}: "
            + ", ".join(f"{name} {pair['harness']} vs {pair['project']}" for name, pair in sorted(differ.items()))
        )
    run["warnings"] += runjson.uv_warnings(environment, cfg["image_uv_version"])
    for step in run["steps"]:
        if step["sampling_violations"]:
            run["warnings"].append(f"{step['step']}: {step['sampling_violations']} request(s) with non-vendor sampling")
    run["harbor"] = {
        "exit_code": harbor.returncode,
        "trial_dir": str(trial_dir) if trial_dir else None,
        "trial_name": (harbor_result or {}).get("trial_name"),
        "reward": ((harbor_result or {}).get("verifier_result") or {}).get("rewards"),
    }
    run["traffic_log"] = str(traffic_dir)
    if trial_dir:
        run_json_path = trial_dir / "run.json"
    failed = [s["step"] for s in run["steps"] if s["end_reason"] == "infra_failure"]
    if failed or infra_notes:
        return finish("infra_failure", "; ".join(infra_notes) or f"infra failure in {failed}", EXIT_INFRA)
    return finish("ok", None, 0)


if __name__ == "__main__":
    sys.exit(main())
