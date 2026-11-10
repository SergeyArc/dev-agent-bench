"""Harbor job plugin: tells the proxy where bench steps begin and end.

Loaded by Harbor on the host (``--plugin bench_harbor_plugin:StepBoundaryPlugin``),
so the evaluated agent cannot reach it. Harbor emits AGENT_START / AGENT_END
once per step of a multi-step task; the step name is the last entry of
``result.step_results`` at that moment.

A failed call to the proxy raises: a step without budget accounting and
traffic attribution must not run silently.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from harbor.models.job.plugin import BaseJobPlugin
from harbor.trial.hooks import TrialEvent, TrialHookEvent


# Libraries whose sources the agent may read from the harness's own Python
# instead of the project's: recorded per environment, never changed.
LIBRARY_PACKAGES = ("sqlalchemy", "asyncpg", "starlette", "fastapi", "pytest-asyncio")
PYTHON_ENVIRONMENTS = ("/opt/openhands-venv", "/opt/openhands-sdk-venv", "/app/.venv")
_VERSIONS_SNIPPET = (
    "import importlib.metadata as m, json, sys\n"
    "def v(n):\n"
    "    try:\n        return m.version(n)\n"
    "    except m.PackageNotFoundError:\n        return None\n"
    "print(json.dumps({'python_version': sys.version.split()[0], 'packages': {n: v(n) for n in sys.argv[1:]}}))\n"
)


class StepBoundaryPlugin(BaseJobPlugin):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._url = os.environ["BENCH_PROXY_CONTROL_URL"].rstrip("/")
        self._token = os.environ["BENCH_PROXY_CONTROL_TOKEN"]
        self._sampler: asyncio.Task[None] | None = None
        self._probe_done = False
        self._probe_failed = False
        self._step: str | None = None

    def _post(self, path: str, payload: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{self._url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-Bench-Control": self._token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()

    async def on_job_start(self, job: Any) -> None:
        job.add_hook(TrialEvent.AGENT_START, self._on_agent_start)
        job.add_hook(TrialEvent.AGENT_END, self._on_agent_end)

    async def on_job_end(self, job_result: Any) -> None:
        return None

    def _record_environment(self, event: TrialHookEvent) -> None:
        """Record the image the trial container runs (Harbor deletes it afterwards)."""
        meta_dir = os.environ.get("BENCH_RUN_META_DIR")
        if not meta_dir or Path(meta_dir, "environment.json").exists():
            return
        info: dict[str, Any] = {"trial_name": event.trial_name}
        try:
            project = event.trial_name.lower()
            listing = subprocess.run(
                ["docker", "ps", "--filter", f"name={project}",
                 "--format", "{{.Image}}\t{{.Names}}"],
                capture_output=True, text=True, timeout=30, check=True,
            ).stdout.split()
            info["image"], info["container"] = listing[0], listing[1]
            info["image_id"] = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", listing[0]],
                capture_output=True, text=True, timeout=30, check=True,
            ).stdout.strip()
            info["architecture"] = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", listing[0]],
                capture_output=True, text=True, timeout=30, check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError, IndexError) as exc:
            # Best effort: the image id is metadata, not a reason to fail a trial.
            info["error"] = repr(exc)
        Path(meta_dir, "environment.json").write_text(json.dumps(info, indent=2))
        if "container" in info:
            self._sampler = asyncio.create_task(
                asyncio.to_thread(self._sample_agent_views, info, Path(meta_dir, "environment.json"))
            )

    @staticmethod
    def _sample_agent_views(info: dict[str, Any], path: Path) -> None:
        """What the agent's own processes resolve as ``uv`` and ``python``.

        A harness may prepend its own directories to PATH, so the PATH is read
        from the environment of the processes running as ``agent`` during the
        first step, and ``uv`` / ``python`` are resolved under each distinct one.
        """
        script = (
            # BENCH_PROBE marks this probe's own processes so they are skipped.
            'for p in $(ps -u agent -o pid=); do e=$(tr "\\0" "\\n" < /proc/$p/environ 2>/dev/null) || continue; '
            'case "$e" in *BENCH_PROBE=1*) continue ;; esac; echo "$e" | grep "^PATH="; done'
            " | sort -u | while read -r line; do p=${line#PATH=}; "
            'printf "%s\\t%s\\t%s\\t%s\\n" "$p" "$(PATH=$p command -v uv)" "$(PATH=$p uv --version 2>&1)" '
            '"$(PATH=$p command -v python)"; done'
        )
        one_shell = (
            'printf "%s\\t%s\\t%s\\t%s\\n" "$PATH" "$(command -v uv)" "$(uv --version 2>&1)" "$(command -v python)"'
        )
        views: dict[str, dict[str, Any]] = {}
        started = time.monotonic()
        for at in (2, 5, 10, 20, 45, 90):
            time.sleep(max(0.0, at - (time.monotonic() - started)))
            result = subprocess.run(
                ["docker", "exec", "-u", "agent", "-e", "BENCH_PROBE=1", info["container"], "bash", "-c", script],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if result.returncode != 0:
                break
            outputs = [("process environment", result.stdout)]
            # Harness installers also edit the agent's shell profiles, which the
            # environment of a running process does not show.
            for source, flag in (("login shell", "-lc"), ("interactive shell", "-ic")):
                shell = subprocess.run(
                    ["docker", "exec", "-u", "agent", "-e", "BENCH_PROBE=1", info["container"], "bash", flag, one_shell],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                outputs.append((source, shell.stdout))
            for source, output in outputs:
                for line in output.splitlines():
                    parts = line.split("\t")
                    if len(parts) == 4:
                        view = views.setdefault(parts[0], dict(zip(("path", "uv", "uv_version", "python"), parts)))
                        view.setdefault("seen_in", [])
                        if source not in view["seen_in"]:
                            view["seen_in"].append(source)
            info["agent_views"] = list(views.values())
            path.write_text(json.dumps(info, indent=2))

    def _record_library_versions(self, when: str) -> None:
        """Versions of LIBRARY_PACKAGES in the harness's Python and in /app/.venv.

        Read-only (package metadata, nothing is imported or changed). Taken
        before the first step and after every step: the agent may change the
        project's environment. Best effort, like the image id.
        """
        meta_dir = os.environ.get("BENCH_RUN_META_DIR")
        if not meta_dir:
            return
        info = json.loads(Path(meta_dir, "environment.json").read_text())
        path = Path(meta_dir, "library_versions.json")
        record = json.loads(path.read_text()) if path.exists() else {"packages": list(LIBRARY_PACKAGES), "snapshots": []}
        snapshot: dict[str, Any] = {"when": when, "environments": {}}
        for root in PYTHON_ENVIRONMENTS:
            if "container" not in info:
                snapshot["error"] = "container is unknown"
                break
            try:
                result = subprocess.run(
                    ["docker", "exec", "-u", "agent", "-w", "/", "-e", "BENCH_PROBE=1", info["container"],
                     f"{root}/bin/python", "-c", _VERSIONS_SNIPPET, *LIBRARY_PACKAGES],
                    capture_output=True, text=True, timeout=60, check=False,
                )
                if result.returncode == 0:
                    snapshot["environments"][root] = json.loads(result.stdout)
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
                snapshot["environments"][root] = {"error": repr(exc)}
        record["snapshots"].append(snapshot)
        path.write_text(json.dumps(record, indent=2))

    def _probe_upstream(self) -> None:
        """From inside the task container, vLLM must not answer without the proxy.

        Asked once, before the agent of the first step starts: without a key
        and with the trial key. Expected: refused (401/403) or unreachable.
        Anything else, or a probe that cannot run, locks the proxy and fails
        the step, so the agent never starts (fail closed).
        """
        url = os.environ.get("BENCH_UPSTREAM_PROBE_URL")
        meta_dir = os.environ.get("BENCH_RUN_META_DIR")
        if not url or not meta_dir or self._probe_done:
            if self._probe_failed:
                raise RuntimeError("bench: upstream probe failed earlier; the agent is not started")
            return
        self._probe_done = True
        info = json.loads(Path(meta_dir, "environment.json").read_text())
        result: dict[str, Any] = {"url": url, "expected": "401/403 or unreachable (000)", "checks": {}}
        ok = "container" in info
        for label, headers in (("no_key", []), ("trial_key", ["-H", f"Authorization: Bearer {os.environ.get('BENCH_PROBE_KEY', '')}"])):
            if not ok and "container" not in info:
                break
            probe = subprocess.run(
                ["docker", "exec", "-u", "agent", info["container"], "curl", "-s", "-o", "/dev/null",
                 "-w", "%{http_code}", "-m", "10", *headers, url],
                capture_output=True, text=True, timeout=30, check=False,
            )
            code = probe.stdout.strip() or f"exit {probe.returncode}"
            result["checks"][label] = code
            ok = ok and code in ("000", "401", "403")
        result["passed"] = ok
        Path(meta_dir, "upstream_probe.json").write_text(json.dumps(result, indent=2))
        if not ok:
            self._probe_failed = True
            self._post("/_control/lockdown", {})
            raise RuntimeError(f"bench: vLLM is reachable from the task container around the proxy: {result}")

    async def _on_agent_start(self, event: TrialHookEvent) -> None:
        steps = event.result.step_results or []
        name = steps[-1].step_name if steps else "single-step"
        self._record_environment(event)
        self._probe_upstream()
        if self._step is None:
            self._record_library_versions(f"before {name}")
        self._step = name
        self._post("/_control/step", {"step": name})

    async def _on_agent_end(self, event: TrialHookEvent) -> None:
        self._post("/_control/step-end", {})
        self._record_library_versions(f"after {self._step}")
