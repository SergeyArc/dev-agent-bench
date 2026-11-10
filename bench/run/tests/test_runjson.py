#!/usr/bin/env python3
"""Self-test of turncats.py, runjson.library_versions, run_batch.py and the
preemption snapshot. No GPU, no Docker. Run: python3 bench/run/tests/test_runjson.py"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import run_batch  # noqa: E402
import run_trial  # noqa: E402
import runjson  # noqa: E402
import turncats  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: Any = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail != "" and not ok else ""))
    if not ok:
        failures.append(name)


SP = "/opt/openhands-venv/lib/python3.13/site-packages"
# (tool, arguments, expected category), in the order of one step.
CALLS: list[tuple[str, dict[str, Any], str]] = [
    ("execute_bash", {"command": f"sed -n 1,40p {SP}/asyncpg/connect_utils.py"}, "library_read"),
    ("terminal", {"command": "grep -rn pool /app/.venv/lib/python3.13/site-packages/sqlalchemy/pool/ | head"}, "library_read"),
    ("bash", {"command": "cd /app && .venv/bin/python -c 'import inspect, starlette.routing as r; print(inspect.getsource(r.Router))'"}, "library_read"),
    ("str_replace_editor", {"command": "view", "path": f"{SP}/starlette/routing.py"}, "library_read"),
    ("read", {"filePath": "/app/.venv/lib/python3.13/site-packages/fastapi/applications.py"}, "library_read"),
    # The project's own install and a bare listing are not library sources.
    ("execute_bash", {"command": "ls /app/.venv/lib/python3.13/site-packages/ | grep -i editable; cat /app/.venv/lib/python3.13/site-packages/_tracksvc.pth"}, "other"),
    ("execute_bash", {"command": "cd /app && uv run python -c 'import asyncpg; print(asyncpg.__version__)'"}, "experiment"),
    ("bash", {"command": "cd /app && uv run python - <<'PY'\nimport asyncio\nprint(1)\nPY"}, "experiment"),
    ("terminal", {"command": "python3 /tmp/try_engine.py"}, "experiment"),
    ("bash", {"command": "echo 'print(1)' | python3"}, "experiment"),
    ("str_replace_editor", {"command": "create", "path": "/app/src/tracksvc/api/app.py", "file_text": "x"}, "file_edit"),
    ("str_replace_editor", {"command": "str_replace", "path": "/app/src/tracksvc/api/app.py", "old_str": "x", "new_str": "y"}, "file_edit"),
    ("execute_bash", {"command": "rm /app/src/tracksvc/api/app.py"}, "rm_recreate"),
    # After `rm` the file is new again: only a second whole-file write counts.
    ("str_replace_editor", {"command": "create", "path": "/app/src/tracksvc/api/app.py", "file_text": "z"}, "file_edit"),
    ("file_editor", {"command": "create", "path": "/app/tests/test_a.py", "file_text": "a"}, "file_edit"),
    ("file_editor", {"command": "create", "path": "/app/tests/test_a.py", "file_text": "b"}, "rm_recreate"),
    ("write", {"filePath": "/app/alembic.ini", "content": "a"}, "file_edit"),
    ("write", {"filePath": "/app/alembic.ini", "content": "b"}, "rm_recreate"),
    ("edit", {"filePath": "/app/alembic.ini", "oldString": "b", "newString": "c"}, "file_edit"),
    ("bash", {"command": "mkdir -p /app/src/tracksvc/domain && cat > /app/src/tracksvc/domain/__init__.py <<'EOF'\n\"\"\"Domain.\"\"\"\nEOF"}, "file_edit"),
    ("execute_bash", {"command": "cd /app && uv run ruff check . && uv run mypy --strict -p tracksvc"}, "checks"),
    ("terminal", {"command": "cd /app && uv run pytest -q 2>&1 | tail -5"}, "checks"),
    ("bash", {"command": "cd /app && DATABASE_URL=x uv run alembic upgrade head"}, "checks"),
    ("execute_bash", {"command": "cd /app && uv run uvicorn tracksvc.api.app:app --port 8000 & sleep 2; curl -s localhost:8000/health"}, "service_run"),
    ("bash", {"command": "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/ready"}, "service_run"),
    ("execute_bash", {"command": "cd /app && git status --short && cat pyproject.toml"}, "other"),
    ("str_replace_editor", {"command": "view", "path": "/app/src/tracksvc/api/app.py"}, "other"),
    ("task_tracker", {"command": "plan", "task_list": []}, "other"),
    ("finish", {"message": "done"}, "other"),
    # A file written by a shell redirect is a file edit, whatever words its content has.
    ("bash", {"command": "cd /app && cat > alembic/env.py << 'EOF'\nfrom alembic import context\nimport pytest\nEOF"}, "file_edit"),
    ("bash", {"command": "cat <<EOF > /app/src/tracksvc/api/main.py\nimport uvicorn\nEOF"}, "file_edit"),
    ("terminal", {"command": "printf '[alembic]\\nscript_location = alembic\\n' > /app/alembic2.ini"}, "file_edit"),
    ("execute_bash", {"command": "echo 'ruff' | tee /app/notes.txt"}, "file_edit"),
    ("bash", {"command": "cat >> /app/notes.txt <<EOF\nmore\nEOF"}, "file_edit"),
    ("bash", {"command": "cd /app && cat > alembic/env.py << 'EOF'\nfrom alembic import context\nEOF"}, "rm_recreate"),
    ("bash", {"command": "rm -f /app/new.py && cat > /app/new.py <<EOF\nx = 1\nEOF"}, "rm_recreate"),
    ("bash", {"command": "cat > /tmp/t.py <<EOF\nprint(1)\nEOF\npython /tmp/t.py"}, "file_edit"),
    # Not file writes: output thrown away or sent to a log.
    ("bash", {"command": "cat /app/pyproject.toml > /dev/null"}, "other"),
    ("bash", {"command": "cd /app && uv run pytest 2>&1 | tee /dev/null"}, "checks"),
    ("bash", {"command": "cd /app && uv run pytest > /tmp/out.txt 2>&1"}, "checks"),
    # A tool name as an argument is not a run of the tool.
    ("terminal", {"command": "uv add fastapi uvicorn"}, "other"),
    ("terminal", {"command": "uv add --dev pytest httpx"}, "other"),
    ("bash", {"command": "grep -rn pytest /app/pyproject.toml; ls /app/alembic/"}, "other"),
    ("bash", {"command": "cd /app && .venv/bin/pytest -q && python -m mypy -p tracksvc"}, "checks"),
    ("bash", {"command": "timeout 20 uv run python -m uvicorn tracksvc.api.app:app"}, "service_run"),
    # OpenHands' Python cell: classified by its code.
    ("execute_ipython_cell", {"code": "content = r\'\'\'import pytest\nfrom alembic import context\n\'\'\'\nwith open('/app/tests/test_cell.py', 'w') as f:\n    f.write(content)"}, "file_edit"),
    ("execute_ipython_cell", {"code": "content = 'x = 2'\nwith open('/app/tests/test_cell.py', 'w') as f:\n    f.write(content)"}, "rm_recreate"),
    ("execute_ipython_cell", {"code": "with open('/app/tests/test_cell.py', 'a') as f:\n    f.write('y = 3')"}, "file_edit"),
    ("execute_ipython_cell", {"code": "from pathlib import Path\nPath(\"/app/src/tracksvc/cell.py\").write_text('x = 1')"}, "file_edit"),
    ("execute_ipython_cell", {"code": "print(open('/app/pyproject.toml').read())"}, "experiment"),
    ("execute_ipython_cell", {"code": "import asyncio, asyncpg\nprint(asyncio.run(asyncpg.connect('postgresql://x')))"}, "experiment"),
    ("execute_ipython_cell", {"code": "import inspect, starlette.routing as r\nprint(inspect.getsource(r.Router))"}, "library_read"),
    ("execute_ipython_cell", {"code": "!cd /app && uv run pytest -q"}, "checks"),
    ("execute_ipython_cell", {"code": "%%bash\ncd /app && uv run ruff check ."}, "checks"),
]


def write_traffic(root: Path) -> None:
    """A traffic log of one step in the proxy's format: both response shapes."""
    step = root / "step-1"
    step.mkdir(parents=True)
    lines = []

    def add(seq: int, exchange: dict[str, Any], entry: dict[str, Any], start: int, took: int) -> None:
        name = f"step-1/{seq:06d}.json"
        exchange["finished_at"] = f"2026-10-03T10:00:{start + took:02d}.000+00:00"
        (root / name).write_text(json.dumps(exchange))
        lines.append(json.dumps({"seq": seq, "step": "step-1", "method": "POST", "file": name, "elapsed_sec": took,
                                 "started_at": f"2026-10-03T10:00:{start:02d}.000+00:00", **entry}))

    def call(name: str, args: dict[str, Any], call_id: str = "c") -> dict[str, Any]:
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

    def history(*results: tuple[str, str, Any]) -> dict[str, Any]:
        """Request body whose history holds these (call id, tool, result content)."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": "task"}]
        for call_id, tool, content in results:
            messages.append({"role": "assistant", "content": None, "tool_calls": [call(tool, {}, call_id)]})
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        return {"messages": messages}

    ok = {"status": 200, "auxiliary": False}
    # 1: plain JSON body (OpenHands), one library read, 100 tokens, 10 s; the next request starts 2 s later.
    add(1, {"response_body": {"choices": [{"message": {"tool_calls": [call(*CALLS[0][:2])]}}]}},
        {**ok, "usage": {"completion_tokens": 100}}, 0, 10)
    # 2: assembled SSE stream (opencode), two calls: a check and an edit, 60 tokens, 4 s, then 6 s of tools.
    r1 = ("c1", "execute_bash", [{"type": "text", "text": "ok\n[Command finished with exit code 0]"}])
    r2 = ("c2", "bash", "5 passed")
    r3 = ("c3", "write", "Wrote file successfully.")
    add(2, {"request_body": history(r1), "response_assembled": {"tool_calls": [
        {"name": "bash", "arguments": json.dumps({"command": "cd /app && uv run pytest"})},
        {"name": "write", "arguments": json.dumps({"filePath": "/app/a.py", "content": "x"})}]}},
        {**ok, "usage": {"completion_tokens": 60}}, 12, 4)
    # 3: auxiliary request (opencode's title) - not a turn.
    summary_request = history(r1, r2, r3)
    summary_request["messages"].append({"role": "user", "content": [{"type": "text", "text": "<EVENT>\nx\n</EVENT>\n\nNow summarize the events using the rules above."}]})
    add(3, {"request_body": summary_request, "response_assembled": {"tool_calls": []}}, {"status": 200, "auxiliary": True, "usage": {"completion_tokens": 999}}, 22, 1)
    # 4: HTTP 400 from vLLM - not a turn.
    overflow = ("This model's maximum context length is 131072 tokens. However, you requested 32000 output tokens and your "
                "prompt contains at least 99073 input tokens, for a total of at least 131073 tokens.")
    add(4, {"response_body": {"error": {"message": overflow, "type": "BadRequestError", "code": 400}}}, {"status": 400, "auxiliary": False}, 24, 1)
    add(7, {"response_body": {"error": {"message": "reasoning_effort: Input should be 'low', 'medium' or 'xhigh'", "code": 400}}},
        {"status": 400, "auxiliary": False}, 32, 1)
    # 10: opencode's final message at its step limit, refused by the Mistral chat template.
    add(10, {"request_body": {"messages": [{"role": "user", "content": "task"}, {"role": "assistant", "content": "CRITICAL - MAXIMUM STEPS REACHED"}]},
             "response_body": {"error": {"message": "Cannot set `add_generation_prompt` to True when the last message is from the assistant.", "code": 400}}},
        {"status": 400, "auxiliary": False, "step": "step-9"}, 50, 1)
    # 5: unparsable arguments, counted and classified as `other`.
    failed = [("c4", "bash", "boom\n[The command completed with exit code 1.]"), ("c5", "file_editor", "ERROR:\nInvalid path"),
              ("c6", "terminal", "[An error occurred during execution.]\n x")]
    add(5, {"request_body": history(r1, r2, r3, *failed), "response_assembled": {"tool_calls": [{"name": "bash", "arguments": "{\"command\": \"ls"}]}},
        {**ok, "usage": {"completion_tokens": 30}}, 26, 2)
    # 8: a degenerate answer: the output limit is spent, the parser made a tool call with garbage as its name.
    garbage = "Let me run the check again:" + "Task completed. " * 20
    offered = [{"type": "function", "function": {"name": "terminal"}}, {"type": "function", "function": {"name": "finish"}}]
    add(8, {"request_body": {"messages": [{"role": "user", "content": "task"}], "tools": offered, "max_tokens": 200},
            "response_body": {"choices": [{"message": {"tool_calls": [call("terminal", {"command": "ls"}), call(garbage, {})]}}]}},
        {**ok, "step": "step-9", "usage": {"completion_tokens": 200}, "response": {"finish_reason": "tool_calls"}}, 40, 5)
    # 9: an answer cut by the limit the proxy added, without a tool call (finish_reason length).
    add(9, {"request_body": {"messages": [{"role": "user", "content": "task"}], "tools": offered}, "proxy_injected": {"max_tokens": 150},
            "response_body": {"choices": [{"message": {"content": None}}]}},
        {**ok, "step": "step-9", "usage": {"completion_tokens": 150}, "response": {"finish_reason": "length"}}, 46, 3)
    # 6: the final answer without a tool call.
    add(6, {"response_body": {"choices": [{"message": {"content": "done"}}]}}, {**ok, "usage": {"completion_tokens": 10}, "response": {"finish_reason": "stop"}}, 30, 1)
    (root / "index.jsonl").write_text("\n".join(lines) + "\n")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    written: set[str] = set()
    for tool, args, wanted in CALLS:
        got = turncats.classify(tool, args, written)
        check(f"classify {tool}: {str(args.get('command') or args.get('path') or args.get('filePath'))[:60]!r} -> {wanted}", got == wanted, got)
    check("library roots: harness and project environments",
          turncats.library_roots(CALLS[0][1]["command"]) == ["/opt/openhands-venv"]
          and turncats.library_roots("grep x .venv/lib/python3.13/site-packages/alembic/config.py") == ["/app/.venv"]
          and turncats.library_roots(CALLS[5][1]["command"]) == [])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_traffic(root)
        summary = turncats.summarize(root, "step-1", "2026-10-03T09:59:50.000+00:00")
        assert summary is not None
        cats = summary["categories"]
        check("turns: auxiliary and failed requests are not turns", summary["turns"] == 4, summary["turns"])
        degenerate = turncats.summarize(root, "step-9")
        assert degenerate is not None
        check("answers at the output limit are counted by tokens, not by finish_reason",
              degenerate["output_limit_hits"] == {"count": 2, "completion_tokens": 350, "finish_reasons": {"tool_calls": 1, "length": 1}, "without_tool_call": 1},
              degenerate["output_limit_hits"])
        check("a tool name that was not offered is flagged, with its length",
              degenerate["invalid_tool_names"]["count"] == 1 and degenerate["invalid_tool_names"]["examples"][0]["name"].startswith("Let me run")
              and degenerate["invalid_tool_names"]["examples"][0]["name_chars"] == 347, degenerate["invalid_tool_names"])
        check("a request ending with the harness's assistant message, refused by the template, is counted apart",
              degenerate["http_errors"]["template_rejected"] == 1 and degenerate["http_errors"]["other"] == 1
              and summary["http_errors"]["template_rejected"] == 0, (degenerate["http_errors"], summary["http_errors"]))
        check("normal answers: no limit hit, no invalid name",
              summary["output_limit_hits"]["count"] == 0 and summary["invalid_tool_names"]["count"] == 0,
              (summary["output_limit_hits"], summary["invalid_tool_names"]))
        check("tool calls counted, unparsed arguments flagged",
              summary["tool_calls"] == 4 and summary["tool_calls_unparsed_arguments"] == 1, summary)
        check("library read: tokens, model and tool seconds",
              cats["library_read"] == {"turns": 1.0, "tool_calls": 1, "completion_tokens": 100, "token_share": 0.5, "model_sec": 10.0, "tools_sec": 2.0},
              cats["library_read"])
        check("a turn with two calls is split equally",
              cats["checks"]["turns"] == 0.5 and cats["checks"]["completion_tokens"] == 30 and cats["checks"]["tools_sec"] == 3.0
              and cats["file_edit"]["turns"] == 0.5 and cats["file_edit"]["model_sec"] == 2.0, (cats["checks"], cats["file_edit"]))
        check("turn without a tool call and unparsable call go to `other`",
              cats["other"]["turns"] == 2.0 and cats["other"]["tool_calls"] == 1 and cats["other"]["completion_tokens"] == 40, cats["other"])
        check("token shares sum to 1", abs(sum(c["token_share"] for c in cats.values()) - 1) < 0.01)
        check("last turn: a message without a tool call is the agent's final answer",
              summary["last_turn"]["final_answer"] is True and summary["last_turn"]["tools"] == [], summary["last_turn"])
        check("last turn: ending on a tool call other than finish is not a final answer",
              degenerate["last_turn"]["final_answer"] is False, degenerate["last_turn"])
        check("longest response", summary["longest_response"] == {"completion_tokens": 100, "model_sec": 10.0}, summary["longest_response"])
        check("library roots counted", summary["library_read_roots"] == {"/opt/openhands-venv": 1}, summary["library_read_roots"])
        check("HTTP 400: context overflow counted apart from other errors",
              summary["http_errors"]["context_overflow"] == 1 and summary["http_errors"]["other"] == 1
              and summary["http_errors"]["other_examples"][0]["message"].startswith("reasoning_effort"), summary["http_errors"])
        check("condenser summaries counted by their prompt, with their tokens",
              summary["condensations"] == {"requests": 1, "answered": 1, "completion_tokens": 999}, summary["condensations"])
        check("tool results: returned and failed counted once each",
              summary["tool_results"] == {"returned": 6, "failed": 3}, summary["tool_results"])
        check("last successful tool call: timed by the request that carried its result",
              summary["last_successful_tool_call"] == {"tool": "write", "result_sent_at": "2026-10-03T10:00:22.000+00:00", "sec_from_step_start": 32.0},
              summary["last_successful_tool_call"])
        check("no tool result -> no last successful call",
              turncats.summarize(root, "step-2", None)["last_successful_tool_call"] is None)  # type: ignore[index]
        check("another step has no turns", turncats.summarize(root, "step-2")["turns"] == 0)  # type: ignore[index]
        check("no traffic log -> None", turncats.summarize(root / "missing", "step-1") is None)

    same = {"sqlalchemy": "2.0.44", "asyncpg": "0.30.0", "starlette": "0.48.0", "fastapi": "0.118.0", "pytest-asyncio": "1.2.0"}
    harness = {**same, "starlette": "0.46.2", "pytest-asyncio": None}
    record = {"packages": list(same), "snapshots": [
        {"when": "before step-1", "environments": {"/opt/openhands-venv": {"packages": harness}, "/app/.venv": {"packages": same}}},
        {"when": "after step-1", "environments": {"/opt/openhands-venv": {"packages": harness}, "/app/.venv": {"packages": {**same, "fastapi": "0.119.0"}}}},
    ]}
    libs = runjson.library_versions(record)
    assert libs is not None
    check("library versions: divergence flagged, missing package is not one",
          libs["diverged"] is True and libs["harness_environment"] == "/opt/openhands-venv"
          and libs["snapshots"][0]["differ"] == {"starlette": {"harness": "0.46.2", "project": "0.48.0"}}, libs)
    check("library versions: change of the project's environment during the step is seen",
          set(libs["snapshots"][1]["differ"]) == {"starlette", "fastapi"}, libs["snapshots"][1]["differ"])
    equal = runjson.library_versions({"snapshots": [{"when": "x", "environments": {"/opt/openhands-sdk-venv": {"packages": same}, "/app/.venv": {"packages": same}}}]})
    check("library versions: equal -> diverged False", equal is not None and equal["diverged"] is False, equal)
    none = runjson.library_versions({"snapshots": [{"when": "x", "environments": {"/app/.venv": {"packages": same}}}]})
    check("library versions: no harness Python (opencode) -> diverged None",
          none is not None and none["diverged"] is None and none["harness_environment"] is None, none)
    check("library versions: nothing recorded -> None", runjson.library_versions(None) is None)

    proxy_step = {"ended_on_upstream_error": False, "refused_budget": 0, "turns": 5}
    harbor_step = {"exception_info": {"exception_type": "AgentTimeoutError"}, "verifier_result": {"rewards": {"reward": 0.5}}}
    check("step over the TTL: end_reason timeout, not infra_failure",
          runjson.step_end_reason(harbor_step, proxy_step, []) == ("timeout", []), runjson.step_end_reason(harbor_step, proxy_step, []))
    waited = {**proxy_step, "upstream_wait_seconds": 700.0, "upstream_waits": 2}
    check("step over the TTL after > 10 min of waiting for the server: infra_failure",
          runjson.step_end_reason(harbor_step, waited, [])[0] == "infra_failure"
          and runjson.step_end_reason(harbor_step, {**waited, "upstream_wait_seconds": 500.0}, []) == ("timeout", []),
          runjson.step_end_reason(harbor_step, waited, []))
    silent = {"exception_info": {"exception_type": "AgentTimeoutError"}, "verifier_result": None}
    check("step over the TTL, verifier wrote nothing, short wait: timeout, not infra_failure",
          runjson.step_end_reason(silent, proxy_step, []) == ("timeout", ["verifier produced no result"])
          and runjson.step_end_reason(silent, {**waited, "upstream_wait_seconds": 600.0}, [])[0] == "timeout",
          runjson.step_end_reason(silent, proxy_step, []))
    check("step over the TTL, verifier wrote nothing, > 10 min of waiting: infra_failure",
          runjson.step_end_reason(silent, waited, [])[0] == "infra_failure", runjson.step_end_reason(silent, waited, []))
    check("no verifier result without a timeout: infra_failure as before",
          runjson.step_end_reason({"exception_info": None, "verifier_result": None}, proxy_step, []) == ("infra_failure", ["verifier produced no result"]),
          runjson.step_end_reason({"exception_info": None, "verifier_result": None}, proxy_step, []))
    with tempfile.TemporaryDirectory() as tmp:
        traffic = Path(tmp)
        entries = [
            {"step": "step-5", "status": 502, "upstream_error": "RemoteDisconnected()", "elapsed_sec": 947.9},
            {"step": "step-5", "status": 502, "upstream_error": "ConnectionRefusedError()", "elapsed_sec": 900.0},
            {"step": "step-5", "status": 200, "elapsed_sec": 86.4, "upstream_waits": [{"error": "x", "waited_sec": 80.5}]},
            {"step": "step-5", "status": 502, "upstream_error": "ConnectionRefusedError()", "elapsed_sec": 900.2,
             "upstream_waits": [{"error": "x", "waited_sec": 900.0, "gave_up": True}]},
            {"step": "step-5", "status": 502, "upstream_error": "BrokenPipeError()", "elapsed_sec": 0.2},
            {"step": "step-4", "status": 502, "upstream_error": "ConnectionRefusedError()", "elapsed_sec": 900.0},
        ]
        (traffic / "index.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        check("waits from the traffic log: failed waits of an older proxy by the time of the request, new ones once",
              runjson.upstream_waits_from_traffic(traffic, "step-5") == (4, 2828.4), runjson.upstream_waits_from_traffic(traffic, "step-5"))
        check("waits from the traffic log: no log -> nothing",
              runjson.upstream_waits_from_traffic(traffic / "none", "step-5") == (0, 0.0) and runjson.upstream_waits_from_traffic(None, "x") == (0, 0.0))
        state = {"steps": {"step-5": {**proxy_step, "upstream_wait_seconds": 80.5, "upstream_waits": 1},
                           "step-1": {**proxy_step, "upstream_wait_seconds": 0.0, "upstream_waits": 0}}}
        result = {"step_results": [{"step_name": "step-1", **harbor_step}, {"step_name": "step-5", **silent}]}
        built = {s["step"]: s for s in runjson.build_steps(None, result, state, ["step-1", "step-5"], traffic, [])}
        check("build_steps: the step over the TTL is failed - reward 0, the verifier's value kept apart",
              built["step-1"]["end_reason"] == "timeout" and built["step-1"]["reward"] == {"reward": 0.0}
              and built["step-1"]["reward_verifier"] == {"reward": 0.5},
              (built["step-1"]["end_reason"], built["step-1"]["reward"], built["step-1"]["reward_verifier"]))
        run_path = traffic / "run.json"
        run_path.write_text(json.dumps({"status": "ok", "harbor": {"reward": {"reward": 0.8}}, "steps": [
            {"step": "step-1", "end_reason": "completed", "reward": {"reward": 0.8}},
            {"step": "step-2", "end_reason": "timeout", "reward": {"reward": 0.0}, "reward_verifier": None}]}))
        row = run_batch.trial_row("openhands", 0, run_path)
        check("batch: a step over the TTL counts as 0 in the reward of the trial, the trial is not rerun",
              row["reward"] == 0.4 and not run_batch.infra_failed(row) and run_batch.next_action(row, "matrix", 0, 0) != "rerun",
              (row["reward"], run_batch.next_action(row, "matrix", 0, 0)))
        check("build_steps: the waits not counted by the proxy are taken from the traffic log -> infra_failure",
              built["step-5"]["end_reason"] == "infra_failure" and built["step-5"]["upstream_wait_seconds"] == 2828.4
              and built["step-5"]["upstream_waits"] == 4 and built["step-5"]["reward"] is None,
              (built["step-5"]["end_reason"], built["step-5"]["end_reason_notes"], built["step-5"]["upstream_wait_seconds"]))

    good = {"step": "step-1", "end_reason": "completed", "reward": 0.9, "tool_calls": 5, "turns": 5, "http_errors": 0,
            "http_errors_context_overflow": 0, "http_errors_other": 0, "upstream_errors": 0, "wall_time_sec": 100.0,
            "completion_tokens": 1000}

    def trial(*steps: dict[str, Any], **top: Any) -> dict[str, Any]:
        return {"harness": "openhands", "repeat": 1, "exit_code": 0, "status": "ok", "steps": list(steps), **top}

    def step(name: str, **change: Any) -> dict[str, Any]:
        return {**good, "step": name, **change}

    clean = trial(step("step-1"), step("step-2"))
    check("batch: a clean trial has no flags and does not stop the batch",
          run_batch.trial_flags(clean) == [] and run_batch.stop_reasons(clean) == [] and run_batch.stop_reasons(clean, "matrix") == [])
    overflow = trial(step("step-1"), step("step-2", http_errors=2, http_errors_context_overflow=2))
    check("batch: context-overflow 400 is not a flag, it is counted in the step line",
          run_batch.trial_flags(overflow) == [] and "| 0 / 2 / 0 |" in run_batch.summary_markdown("m", [overflow]))
    other_400 = {"http_errors": 1, "http_errors_other": 1, "http_errors_other_examples": [{"status": 400, "message": "reasoning_effort: bad"}]}
    for label, bad, stops_matrix in (
        ("crashed", trial(step("step-1"), step("step-3", end_reason="crashed")), False),
        ("another HTTP 400 from vLLM", trial(step("step-1"), step("step-2", **other_400)), False),
        ("no tool call parsed", trial(step("step-1"), step("step-4", tool_calls=0)), False),
        ("infra_failure of a step", trial(step("step-1"), step("step-2", end_reason="infra_failure"), status="infra_failure", exit_code=2), True),
        ("infra_failure of the trial", trial(step("step-1"), status="infra_failure", status_reason="Harbor exit code 1", exit_code=2), True),
        ("trial did not start", trial(status="infra_failure", status_reason="tunnel", exit_code=3), True),
    ):
        flags = run_batch.trial_flags(bad)
        check(f"batch: {label} is flagged and stops a review-mode batch", len(flags) == 1 and run_batch.stop_reasons(bad) == flags, flags)
        check(f"batch --matrix: {label} " + ("stops" if stops_matrix else "does not stop") + " the batch",
              bool(run_batch.stop_reasons(bad, "matrix")) is stops_matrix, run_batch.stop_reasons(bad, "matrix"))
    final_cats = {"last_turn": {"final_answer": True}, "http_errors": {"context_overflow": 0}}
    cut_cats = {"last_turn": {"final_answer": False}, "http_errors": {"context_overflow": 9}}
    check("turn limit: finished by itself well under the limit",
          runjson.turn_limit_flags(final_cats, {"turns": 71, "history_shrinks": 0}, 300)
          == {"no_final_answer": False, "turn_limit_hit": False, "iterations": 71, "template_rejected_harness_message": False})
    check("turn limit: turns + overflow answers + condensations reach the limit, no final answer",
          runjson.turn_limit_flags(cut_cats, {"turns": 282, "history_shrinks": 9}, 300)
          == {"no_final_answer": True, "turn_limit_hit": True, "iterations": 300, "template_rejected_harness_message": False})
    rejected_cats = {"last_turn": {"final_answer": False}, "http_errors": {"context_overflow": 0, "other": 1, "template_rejected": 1}}
    check("turn limit: 299 turns and the harness's final message rejected by the chat template -> limit hit, flagged",
          runjson.turn_limit_flags(rejected_cats, {"turns": 299, "history_shrinks": 0}, 300)
          == {"no_final_answer": True, "turn_limit_hit": True, "iterations": 300, "template_rejected_harness_message": True})
    check("turn limit: a larger limit given to the harness is the one compared with",
          runjson.turn_limit_flags(cut_cats, {"turns": 282, "history_shrinks": 9}, 600)["turn_limit_hit"] is False)
    check("turn limit: cut short early without a final answer is not a limit hit",
          runjson.turn_limit_flags(cut_cats, {"turns": 12, "history_shrinks": 0}, 300)["turn_limit_hit"] is False
          and runjson.turn_limit_flags(cut_cats, {"turns": 12, "history_shrinks": 0}, 300)["no_final_answer"] is True)
    check("turn limit: no turns or unknown limit -> None",
          runjson.turn_limit_flags(None, {"turns": 0}, 300)["no_final_answer"] is None
          and runjson.turn_limit_flags(final_cats, {"turns": 5}, None)["turn_limit_hit"] is None)
    check("batch: turn limit and missing final answer are flags, never stops in --matrix",
          run_batch.trial_flags(trial(step("step-1"), step("step-2", turn_limit_hit=True, no_final_answer=True, iterations=300)))
          == ["step-2: turn limit hit (300 iterations)"]
          and run_batch.trial_flags(trial(step("step-1", no_final_answer=True, turn_limit_hit=False))) == ["step-1: ended without a final answer"]
          and run_batch.stop_reasons(trial(step("step-1", turn_limit_hit=True, no_final_answer=True, iterations=300)), "matrix") == [])
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        for step_name, log in (
            ("step-1", "INFO agent_controller.py - state RUNNING\nWARNING\x1b[0m: stuck.py:162 - Action, Observation loop detected\n"
                       "ERROR AgentStuckInLoopError: Agent got stuck in a loop\n"),
            ("step-2", "INFO agent_controller.py - reached maximum iteration. Current iteration: 300\n"),
            ("step-3", "WARNING: stuck.py:210 - Action, Observation pattern detected\n"),
        ):
            where = base / "steps" / step_name / "agent"
            where.mkdir(parents=True)
            (where / "openhands.txt").write_text(log)
        (base / "steps" / "step-4" / "agent").mkdir(parents=True)
        got = {n: runjson.loop_detected(base, n) for n in ("step-1", "step-2", "step-3", "step-4")}
        check("loop detector: AgentStuckInLoopError in the harness log, with the detector's message",
              got["step-1"] == {"detected": True, "message": "Action, Observation loop detected"}, got["step-1"])
        check("loop detector: the iteration limit is not a loop", got["step-2"] == {"detected": False, "message": None}, got["step-2"])
        check("loop detector: `pattern detected` counts too", got["step-3"]["detected"] is True and "pattern detected" in got["step-3"]["message"], got["step-3"])
        check("loop detector: no harness log -> None", got["step-4"]["detected"] is None and runjson.loop_detected(None, "step-1")["detected"] is None)
    check("batch: a step stopped by the loop detector is flagged with the detector's message, never a stop in --matrix",
          run_batch.trial_flags(trial(step("step-2", no_final_answer=True, turn_limit_hit=False, loop_detected=True,
                                           loop_detected_message="Action, Observation loop detected")))
          == ["step-2: stopped by the harness's loop detector (Action, Observation loop detected)"]
          and run_batch.stop_reasons(trial(step("step-2", loop_detected=True)), "matrix") == [])
    for kind in ("fallback", "missing"):
        nosnap = trial(step("step-1"), step("step-2", snapshot=kind, snapshot_error="git bundle failed"))
        check(f"batch: code snapshot {kind} is a flag, never a stop in --matrix",
              run_batch.trial_flags(nosnap) == [f"step-2: code snapshot {kind} (git bundle failed)"]
              and run_batch.stop_reasons(nosnap, "matrix") == [], run_batch.trial_flags(nosnap))
    check("batch: snapshot ok is not a flag", run_batch.trial_flags(trial(step("step-1", snapshot="ok"))) == [])
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        def artifacts(step_name: str, meta: dict[str, Any] | None, *files: str) -> None:
            where = base / "steps" / step_name / "artifacts" / "logs" / "artifacts"
            where.mkdir(parents=True)
            if meta is not None:
                (where / f"snapshot-{step_name}.json").write_text(json.dumps(meta))
            for name in files:
                (where / name).write_bytes(b"data")

        artifacts("step-1", {"status": "ok", "error": ""}, "snapshot-step-1.bundle")
        artifacts("step-2", {"status": "fallback", "error": "git bundle failed"}, "snapshot-step-2.tar.gz")
        artifacts("step-3", {"status": "error", "error": "git bundle failed"})
        artifacts("step-4", None)
        artifacts("step-5", {"status": "ok", "error": ""})  # the hook said ok, the bundle was not collected
        got = {n: runjson.snapshot_status(base, n) for n in ("step-1", "step-2", "step-3", "step-4", "step-5")}
        check("snapshot status: bundle -> ok", got["step-1"]["status"] == "ok" and got["step-1"]["bundle"] and got["step-1"]["error"] is None, got["step-1"])
        check("snapshot status: archive only -> fallback, with git's error",
              got["step-2"]["status"] == "fallback" and got["step-2"]["archive"] and got["step-2"]["error"] == "git bundle failed", got["step-2"])
        check("snapshot status: neither file -> missing, with the error",
              got["step-3"]["status"] == "missing" and got["step-3"]["error"] == "git bundle failed", got["step-3"])
        check("snapshot status: no metadata -> missing", got["step-4"]["status"] == "missing" and "no snapshot metadata" in got["step-4"]["error"], got["step-4"])
        check("snapshot status: hook said ok but no bundle was collected -> missing",
              got["step-5"]["status"] == "missing" and got["step-5"]["hook_status"] == "ok", got["step-5"])
        check("snapshot status: no trial directory -> missing", runjson.snapshot_status(None, "step-1")["status"] == "missing")

    late = trial(step("step-1"), step("step-2"), step("step-5", end_reason="crashed"))
    check("batch: a flag of a later step names the step", run_batch.trial_flags(late) == ["step-5: end_reason crashed"], run_batch.trial_flags(late))
    check("batch: budget_exhausted and timeout are results, not flags",
          run_batch.trial_flags(trial(step("step-1", end_reason="timeout"), step("step-2", end_reason="budget_exhausted"))) == [])

    check("batch: `not started` = no model request was made",
          run_batch.not_started(trial(status="infra_failure", exit_code=3))
          and run_batch.not_started(trial(step("step-1", end_reason="infra_failure", turns=None), status="infra_failure", exit_code=2))
          and not run_batch.not_started(trial(step("step-1", end_reason="infra_failure", turns=7), status="infra_failure", exit_code=2))
          and not run_batch.not_started(trial(step("step-1", turns=7))))

    infra = trial(step("step-1"), step("step-5", end_reason="infra_failure"), status="infra_failure", exit_code=2)
    crashed = trial(step("step-1"), step("step-3", end_reason="crashed"))
    never = trial(status="infra_failure", exit_code=3)
    check("batch --matrix: a started trial that ended infra_failure is rerun, up to 2 times",
          [run_batch.next_action(infra, "matrix", 0, n) for n in (0, 1, 2)] == ["retry_infra", "retry_infra", "done"])
    check("batch --matrix: an unstarted trial gets 3 attempts in all",
          [run_batch.next_action(never, "matrix", n, 0) for n in (1, 2, 3)] == ["retry_unstarted", "retry_unstarted", "done"])
    check("batch --matrix: crashed and clean trials are not rerun; review mode reruns nothing",
          run_batch.next_action(crashed, "matrix", 0, 0) == "done" and run_batch.next_action(clean, "matrix", 0, 0) == "done"
          and run_batch.next_action(infra, "review", 0, 0) == "done" and run_batch.next_action(never, "review", 1, 0) == "done")
    late_port = free_port()
    check("server wait: gives up when the server does not answer in time",
          run_batch.wait_for_server(f"http://127.0.0.1:{late_port}", max_wait=1.0, interval=0.3) is None)
    delayed = subprocess.Popen([sys.executable, "-c",
                                "import subprocess, sys, time; time.sleep(1.5); "
                                f"subprocess.run([sys.executable, {str(HERE / 'mock_vllm.py')!r}, '--port', '{late_port}', '--api-key', 'k'])"])
    try:
        waited = run_batch.wait_for_server(f"http://127.0.0.1:{late_port}", max_wait=20.0, interval=0.3)
        check("server wait: returns when /v1/models answers again (401 without a key counts)",
              waited is not None and 1.0 <= waited < 15.0, waited)
    finally:
        subprocess.run(["pkill", "-f", f"mock_vllm.py --port {late_port}"], check=False)
        delayed.terminate()
        delayed.wait()

    # The opencode case: exit code 1 after a context overflow it recovered from.
    exit1 = {"exception_type": "NonZeroAgentExitCodeError", "exception_message": "Command failed (exit 1): opencode ..."}
    overflowed = {"http_errors": {"context_overflow": 1, "last_context_overflow_seq": 217, "other": 0}}
    final = {"last_response": {"seq": 246, "finish_reason": "stop", "tool_calls": 0}}
    check("end reason: non-zero exit after a recovered context overflow, final answer given -> finished",
          runjson.finished_after_overflow("crashed", exit1, final, overflowed))
    check("end reason: the same whatever Harbor names the non-zero exit",
          all(runjson.finished_after_overflow("crashed", {**exit1, "exception_type": kind}, final, overflowed)
              for kind in ("ApiOverloadedError", "NetworkConnectionError")))
    for label, args in (
        ("no overflow in the step", ("crashed", exit1, final, {"http_errors": {"context_overflow": 0, "last_context_overflow_seq": None, "other": 0}})),
        ("nothing after the overflow", ("crashed", exit1, {"last_response": {"seq": 216, "finish_reason": "stop", "tool_calls": 0}}, overflowed)),
        ("last answer is a tool call", ("crashed", exit1, {"last_response": {"seq": 246, "finish_reason": "tool_calls", "tool_calls": 1}}, overflowed)),
        ("another HTTP error too", ("crashed", exit1, final, {"http_errors": {"context_overflow": 1, "last_context_overflow_seq": 217, "other": 1}})),
        ("killed by a signal", ("crashed", {**exit1, "exception_message": "Command failed (exit 143): opencode"}, final, overflowed)),
        ("not a harness exit", ("crashed", {"exception_type": "RuntimeError", "exception_message": "x"}, final, overflowed)),
        ("a timeout", ("timeout", exit1, final, overflowed)),
    ):
        check(f"end reason: stays as it is - {label}", not runjson.finished_after_overflow(*args))

    # A trial that does not start: --matrix tries 3 times, review mode once. No Docker: the upstream is down.
    def unstarted(*flags: str) -> tuple[int, dict[str, Any], float]:
        with tempfile.TemporaryDirectory() as out:
            started = time.monotonic()
            done = subprocess.run(
                [sys.executable, str(HERE.parent / "run_batch.py"), "--model", "gemma-4-31b", "--harnesses", "opencode", "openhands",
                 "--repeats", "2", "--steps", "5", "--upstream", "http://127.0.0.1:9", "--out", out, "--no-caffeinate", *flags],
                capture_output=True, text=True, env={**os.environ, "BENCH_VLLM_API_KEY": "dummy"}, check=False,
            )
            batch = json.loads(next(Path(out).glob("batch-*/batch.json")).read_text())
            return done.returncode, batch, time.monotonic() - started

    code, batch, took = unstarted("--matrix", "--retry-wait", "1")
    first = batch["trials"][0]
    check("batch --matrix: an unstarted trial is tried 3 times with a pause, then the batch stops",
          code == 3 and len(batch["trials"]) == 1 and [a["attempt"] for a in first["attempts"]] == [1, 2, 3]
          and all(a["exit_code"] == 3 for a in first["attempts"]) and len({a["run_json"] for a in first["attempts"]}) == 3
          and first["stop_reasons"] == ["the trial did not start"] and took >= 2, (code, first.get("attempts"), took))
    code, batch, _ = unstarted()
    check("batch (review mode): an unstarted trial is not retried",
          code == 3 and len(batch["trials"]) == 1 and len(batch["trials"][0]["attempts"]) == 1, (code, batch["trials"][0].get("attempts")))

    condenser = {"kind": "LLMSummarizingCondenser", "max_size": 80, "keep_first": 4}
    late["flags"], late["stop_reasons"] = run_batch.trial_flags(late), []
    second = trial(step("step-1", history_shrinks=2, condensations=3, condenser=condenser), harness="openhands-sdk", repeat=2,
                   preemptions={"during_trial": None, "after": None, "command": "curl X"})
    table = run_batch.summary_markdown("m", [late, second])
    check("batch: summary has a line per trial and a line per step",
          table.count("| openhands #1 |") == 1 and table.count("| openhands-sdk #2 |") == 1
          and all(f"| {name} | " in table for name in ("step-1", "step-2", "step-5"))
          and "0.9 0.9 0.9" in table and "completed completed crashed" in table and "| 300 | 3000 |" in table, table)
    check("batch: flags are in the summary even when they did not stop the batch", "step-5: end_reason crashed" in table, table)
    check("batch: summary shows the condenser and the number of summaries",
          "| 2 / 3 |" in table and "Condenser: LLMSummarizingCondenser max_size=80 keep_first=4" in table, table)
    check("batch: summary prints the server command when /metrics was not read", "curl X" in table, table)
    environment = {"agent_views": [{"uv": "/home/agent/.local/bin/uv", "uv_version": "uv 0.12.22", "seen_in": ["login shell"]}]}
    check("uv: the pinned installer's own copy of the image version is not a warning", runjson.uv_warnings(environment, "0.12.22") == [])
    environment["agent_views"][0]["uv_version"] = "uv 0.12.23"
    check("uv: another version is a warning", len(runjson.uv_warnings(environment, "0.12.22")) == 1)

    cfg = json.loads((HERE.parent / "harnesses.json").read_text())
    raised = json.loads(json.dumps(cfg))
    raised["limits"]["max_turns"] = 600
    built = {h: run_trial.agent_config(h, raised, "m", 131072, {"temperature": 1.0}, "http://x/v1", "k", 7200.0) for h in ("openhands", "openhands-sdk", "opencode")}
    check("--max-turns reaches every harness: OpenHands max_iterations, opencode agent.build.steps",
          built["openhands"]["kwargs"]["max_iterations"] == 600 and built["openhands-sdk"]["kwargs"]["max_iterations"] == 600
          and built["opencode"]["kwargs"]["opencode_config"]["agent"]["build"]["steps"] == 600,
          {h: b["kwargs"].get("max_iterations") for h, b in built.items()})
    check("step TTL is 2 h, budget 1M tokens",
          cfg["limits"]["agent_timeout_sec"] == 7200 and cfg["limits"]["token_budget_per_step"] == 1_000_000, cfg["limits"])

    port = free_port()
    mock = subprocess.Popen([sys.executable, str(HERE / "mock_vllm.py"), "--port", str(port), "--preemptions", "3"])
    try:
        for _ in range(100):
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        snapshot = run_trial.vllm_preemptions(f"http://127.0.0.1:{port}")
        check("preemptions read from /metrics", snapshot["value"] == 3.0 and "command" not in snapshot, snapshot)
    finally:
        mock.terminate()
        mock.wait()
    snapshot = run_trial.vllm_preemptions(f"http://127.0.0.1:{port}")
    check("preemptions: unreachable /metrics -> no value, command for the server",
          snapshot["value"] is None and "num_preemptions_total" in snapshot["command"], snapshot)

    # Requests refused outside a step: right after an agent timeout they are the agent's, not the infrastructure's.
    with tempfile.TemporaryDirectory() as tmp:
        traffic = Path(tmp)

        def refusals(at: list[str], exception: str | None = "AgentTimeoutError", requests: int | None = None) -> dict[str, int]:
            (traffic / "index.jsonl").write_text("".join(
                json.dumps({"step": "_outside-step", "started_at": moment, "status": 403, "refused": "no_active_step"}) + "\n"
                for moment in at))
            state = {
                "steps": {
                    "step-1": {"started_at": "2026-10-09T03:00:00.000+00:00", "ended_at": "2026-10-09T05:00:00.000+00:00"},
                    "step-2": {"started_at": "2026-10-09T05:10:00.000+00:00", "ended_at": "2026-10-09T05:40:00.000+00:00"},
                },
                "outside_step": {"requests": len(at) if requests is None else requests},
            }
            result = {"step_results": [
                {"step_name": "step-1", "exception_info": {"exception_type": exception} if exception else None},
                {"step_name": "step-2", "exception_info": None},
            ]}
            return runjson.outside_step_refusals(traffic, state, result)

        check("outside step: a request refused right after an agent timeout is not an infrastructure failure",
              refusals(["2026-10-09T05:00:00.500+00:00"]) == {"after_agent_timeout": 1, "other": 0})
        check("outside step: the step did not time out -> infrastructure",
              refusals(["2026-10-09T05:00:00.500+00:00"], exception=None) == {"after_agent_timeout": 0, "other": 1}
              and refusals(["2026-10-09T05:00:00.500+00:00"], exception="NonZeroAgentExitCodeError") == {"after_agent_timeout": 0, "other": 1})
        check("outside step: after a later step that did not time out, or before the first step -> infrastructure",
              refusals(["2026-10-09T05:41:00.000+00:00"]) == {"after_agent_timeout": 0, "other": 1}
              and refusals(["2026-10-09T02:59:00.000+00:00"]) == {"after_agent_timeout": 0, "other": 1})
        check("outside step: each refusal is placed on its own",
              refusals(["2026-10-09T05:00:00.500+00:00", "2026-10-09T05:41:00.000+00:00"]) == {"after_agent_timeout": 1, "other": 1})
        check("outside step: a refusal the traffic log does not show stays infrastructure",
              refusals(["2026-10-09T05:00:00.500+00:00"], requests=2) == {"after_agent_timeout": 1, "other": 1})
        check("outside step: no refusals, or no traffic log -> nothing excused",
              refusals([]) == {"after_agent_timeout": 0, "other": 0}
              and runjson.outside_step_refusals(None, {"outside_step": {"requests": 1}}, None) == {"after_agent_timeout": 0, "other": 1})

    print(f"\n{'OK' if not failures else 'FAILED'}: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
