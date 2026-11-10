"""Thin wrappers around Harbor's agents: the instruction goes through a file.

Harbor 0.23.0 puts the whole instruction on the harness command line. The
instruction contains the service start command, so an agent that stops its
service with ``pkill -f "uvicorn tracksvc.api.app:app"`` also kills its own
harness. These subclasses keep the harnesses and their options unchanged and
only rewrite the launch command so that no process has the instruction in its
argv:

* opencode   - ``opencode run`` reads the prompt from stdin;
* OpenHands  - ``openhands.core.main --file <path>``;
* SDK        - a small launcher reads the file and calls Harbor's runner
               in-process; the launcher also sets the LLM request timeout,
               which Harbor's runner does not expose, and gives the agent the
               history condenser of the OpenHands product (see below).

Condenser of the SDK agent. Harbor's runner builds ``Agent(llm, tools,
agent_context)`` without a condenser, so a full context window ends the run
with ContextWindowExceededError. The OpenHands CLI builds its agent with
``get_default_condenser(llm.model_copy(update={"usage_id": "condenser"}))``,
which is ``openhands.sdk.context.condenser.default_condenser``: an
LLMSummarizingCondenser on the same model with the preset sizes of the
installed SDK (1.50.1: max_size=80, keep_first=4, no token limit). The
launcher does the same and writes what it configured to
``/logs/agent/bench-condenser.json``.

uv of the OpenHands installers. Harbor installs uv with the unversioned
``https://astral.sh/uv/install.sh`` (the latest release, into ~/.local/bin of
the agent, which the agent's shells then prefer). ``BENCH_UV_VERSION`` (set by
run_trial.py from ``image_uv_version``) pins the installer to that version.

The rewrite is a string replacement on Harbor's own command. If the expected
fragment is missing (another Harbor version), the run fails loudly instead of
silently falling back to the argv form.

Used through ``agents[].import_path``, e.g. ``bench_agents:BenchOpenCode``.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

from harbor.agents.installed.opencode import OpenCode
from harbor.agents.installed.openhands import OpenHands
from harbor.agents.installed.openhands_sdk import OpenHandsSDK
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

# Outside /app (not part of the agent's work tree) and readable by the agent.
INSTRUCTION_PATH = "/installed-agent/bench-task.md"
SDK_LAUNCHER_PATH = "/installed-agent/bench_launch_sdk.py"
# Every Harbor launch command of the three harnesses pipes into this.
LAUNCH_MARKER = "tee /logs/agent/"
UV_INSTALLER = "https://astral.sh/uv/install.sh"
CONDENSER_INFO_PATH = "/logs/agent/bench-condenser.json"

SDK_LAUNCHER = '''\
"""Runs Harbor's OpenHands SDK runner with the instruction read from a file."""
import json
import os
import runpy
import sys

import openhands.sdk as sdk
from openhands.sdk.context.condenser import default_condenser

timeout = int(os.environ["BENCH_LLM_TIMEOUT_SEC"])
_LLM = sdk.LLM
_Agent = sdk.Agent


def _llm(**kwargs):
    kwargs.setdefault("timeout", timeout)
    return _LLM(**kwargs)


def _agent(**kwargs):
    # The condenser of the OpenHands CLI: the SDK preset on a copy of the agent's LLM.
    if kwargs.get("condenser") is None:
        condenser = default_condenser(kwargs["llm"].model_copy(update={"usage_id": "condenser"}))
        kwargs["condenser"] = condenser
        info = {
            "kind": type(condenser).__name__,
            "max_size": condenser.max_size,
            "keep_first": condenser.keep_first,
            "max_tokens": condenser.max_tokens,
            "llm_model": condenser.llm.model,
            "llm_base_url": condenser.llm.base_url,
            "source": "openhands.sdk.context.condenser.default_condenser",
            "sdk_version": getattr(sdk, "__version__", None),
        }
        with open("__CONDENSER_INFO_PATH__", "w", encoding="utf-8") as handle:
            json.dump(info, handle, indent=2)
    return _Agent(**kwargs)


sdk.LLM = _llm
sdk.Agent = _agent
with open(sys.argv[1], encoding="utf-8") as handle:
    instruction = handle.read()
# sys.argv is rewritten in memory only; /proc/<pid>/cmdline keeps the file path.
sys.argv = ["/installed-agent/run_agent.py", f"--instruction={instruction}", *sys.argv[2:]]
runpy.run_path("/installed-agent/run_agent.py", run_name="__main__")
'''.replace("__CONDENSER_INFO_PATH__", CONDENSER_INFO_PATH)


class _InstructionFileMixin:
    """Uploads the instruction to a file and rewrites the launch command."""

    _bench_quoted: str | None = None

    def _bench_rewrite(self, command: str, quoted: str) -> str:
        raise NotImplementedError

    async def _bench_upload(self, environment: BaseEnvironment, text: str, target: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "payload"
            source.write_text(text, encoding="utf-8")
            await environment.upload_file(source_path=source, target_path=target)
        # Uploaded as root: make it readable for the agent user, nothing more.
        await environment.exec(command=f"chmod 0644 {shlex.quote(target)}", user="root")

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        await environment.exec(command="mkdir -p /installed-agent", user="root")
        await self._bench_upload(environment, instruction, INSTRUCTION_PATH)
        self._bench_quoted = shlex.quote(instruction)
        try:
            await super().run(instruction, environment, context)  # type: ignore[misc]
        finally:
            self._bench_quoted = None

    async def exec_as_agent(self, environment: BaseEnvironment, command: str, **kwargs: Any) -> Any:
        version = os.environ.get("BENCH_UV_VERSION")
        if version and UV_INSTALLER in command:
            command = _replace_once(command, UV_INSTALLER, f"https://astral.sh/uv/{version}/install.sh")
        quoted = self._bench_quoted
        if quoted is not None and quoted in command:
            command = self._bench_rewrite(command, quoted)
            if quoted in command:
                raise RuntimeError("bench: the instruction is still on the harness command line")
        elif quoted is not None and LAUNCH_MARKER in command:
            # The launch command without the text we uploaded (e.g. a prompt
            # template rewrote it): refuse rather than run with another prompt.
            raise RuntimeError("bench: harness launch command does not carry the uploaded instruction")
        return await super().exec_as_agent(environment, command=command, **kwargs)  # type: ignore[misc]


def _replace_once(command: str, old: str, new: str) -> str:
    if command.count(old) != 1:
        raise RuntimeError(f"bench: unexpected harness command, cannot move the instruction to a file: {old[:40]!r}")
    return command.replace(old, new)


class BenchOpenCode(_InstructionFileMixin, OpenCode):
    def _bench_rewrite(self, command: str, quoted: str) -> str:
        # `opencode run` appends piped stdin to the (now empty) message.
        command = _replace_once(command, f"-- {quoted} ", "")
        return _replace_once(command, "</dev/null", f"<{INSTRUCTION_PATH}")


class BenchOpenHands(_InstructionFileMixin, OpenHands):
    def _bench_rewrite(self, command: str, quoted: str) -> str:
        return _replace_once(command, f"--task={quoted}", f"--file={INSTRUCTION_PATH}")


class BenchOpenHandsSDK(_InstructionFileMixin, OpenHandsSDK):
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        await environment.exec(command="mkdir -p /installed-agent", user="root")
        await self._bench_upload(environment, SDK_LAUNCHER, SDK_LAUNCHER_PATH)
        await super().run(instruction, environment, context)

    def _bench_rewrite(self, command: str, quoted: str) -> str:
        command = _replace_once(command, "/installed-agent/run_agent.py", f"{SDK_LAUNCHER_PATH} {INSTRUCTION_PATH}")
        return _replace_once(command, f"--instruction={quoted}", "")
