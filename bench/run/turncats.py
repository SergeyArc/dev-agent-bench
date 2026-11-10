#!/usr/bin/env python3
"""Turn categories of a bench step, computed from the proxy traffic log.

    python3 bench/run/turncats.py <traffic dir> [step-1]

What a turn was spent on is read from the tool calls of the model's answer,
the same way for all three harnesses (the proxy log holds the assembled
message of an SSE stream as well as a plain JSON body):

  library_read  reading the sources of installed libraries (a path under
                ``site-packages`` that is not the project's own distribution,
                or ``inspect.getsource`` and the like);
  experiment    throwaway Python: ``python -c``, a heredoc or a pipe into
                python, a script under /tmp;
  file_edit     the first creation of a file and edits in place, with an editor
                tool or by a shell redirect (``cat > f``, ``cat <<EOF > f``,
                ``tee f``, ``printf … > f``);
  rm_recreate   ``rm`` of a file, and writing a whole file that was already
                written in this step (OpenHands' editor does not overwrite, so
                the model removes and re-creates; opencode's ``write`` and a
                shell ``cat > f`` rewrite);
  checks        ruff, mypy, pytest, alembic, import-linter;
  service_run   uvicorn / the worker, and curl against the local service;
  other         everything else (listing and reading project files, git,
                planning tools, the final message, turns without a tool call).

A Python cell (`execute_ipython_cell` of OpenHands) is classified by its code:
a cell that writes a file (`open(path, "w")`, `Path(path).write_text(...)`) is
file_edit / rm_recreate, shell lines (`!cmd`, `%%bash`) follow the shell rules,
reading library sources is library_read, anything else is experiment.

A shell command that writes a file is file_edit / rm_recreate whatever else it
mentions (the file's content is full of words like `alembic` or `uvicorn`).
Any other shell command that fits several categories gets the first one in the
order library_read, service_run, checks, experiment, rm_recreate, file_edit. A turn
with several tool calls is split equally between them, so `turns` may be
fractional. Seconds: `model_sec` is the generation time of the turn,
`tools_sec` the gap until the harness sent its next request (tool execution
plus harness overhead).

`last_successful_tool_call`: the last tool call whose result the harness sent
back to the model without a failure mark, timed by the request that carried
the result. Failure marks: a non-zero `exit code N` (OpenHands), `[An error
occurred during execution.]`, a result that starts with `ERROR` / `Error:`.
opencode does not report the exit code of a shell command, so a failed command
of opencode counts as successful here.

`condensations`: summarizing requests of the OpenHands SDK condenser, found by
the last line of its prompt (`requests`, `answered`, `completion_tokens`).

`output_limit_hits`: answers whose completion tokens reached the output limit
of the request (`max_tokens` / `max_completion_tokens`, sent by the harness or
added by the proxy), whatever `finish_reason` says: a degenerate answer can end
as `tool_calls` when the parser makes a tool call out of it.
`invalid_tool_names`: tool calls of an answer whose name is not one of the
tools offered in the request (sending such a call back makes vLLM answer 400).

`last_turn`: the tool names and `finish_reason` of the last turn, and
`final_answer`: whether the step ended with the agent's own final answer - a
`finish` tool call (OpenHands) or a message without a tool call
(`finish_reason: stop`, opencode). A step that ends on any other tool call was
stopped from outside (turn limit, TTL, crash).

`http_errors`: 4xx answers of vLLM, split into `context_overflow` (HTTP 400,
"maximum context length": prompt + requested output > max_model_len; the
harness reacts by compacting the history) and `other` (with examples).
`template_rejected` counts the `other` ones where the chat template refused a
request whose last message is the harness's own `assistant` message (HTTP 400
"… the last message is from the assistant"): opencode ends a session at its
step limit with such a message ("MAXIMUM STEPS REACHED … respond with text
only"), which the Mistral template does not accept.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

CATEGORIES = ("library_read", "experiment", "file_edit", "rm_recreate", "checks", "service_run", "other")

SHELL_TOOLS = {"bash", "execute_bash", "terminal"}
PYTHON_CELL_TOOLS = {"execute_ipython_cell"}
# tool name -> argument holding the sub-command; `None`: the tool name is the action.
EDITOR_TOOLS = {"str_replace_editor": "command", "file_editor": "command", "write": None, "edit": None, "read": None}
PATH_KEYS = ("path", "filePath", "file_path")

# A library under site-packages: not the bare directory, not the project's own install.
_LIB_PATH = re.compile(
    r"(?:site|dist)-packages/(?!_*tracksvc|__editable__|__pycache__)(?P<pkg>[A-Za-z0-9][A-Za-z0-9_]*)(?=[/.])"
)
_VENV_ROOT = re.compile(r"(?P<root>[\w./-]+?)/lib/python[\d.]+/(?:site|dist)-packages/")
_LIB_INTROSPECTION = re.compile(r"inspect\.(?:getsource|getsourcefile|getsourcelines|getfile)\b")
_PYTHON = r"(?:[\w./-]*/)?python[\d.]*"
_EXPERIMENT = re.compile(
    rf"(?:^|[\s;&|(]){_PYTHON}\s+(?:-[A-Za-z]*c\b|-\s*(?:<<|$|[;&|])|<<|/tmp/\S+\.py)|\|\s*{_PYTHON}\s*(?:-\s*)?(?:$|[;&|])"
)
# A tool counts only where a command starts: after `;`, `&&`, `|`, `(`, an env assignment,
# `uv run`, `python -m`, `timeout N` or a path prefix - not as an argument (`uv add pytest`).
_CMD = r"(?:^|[;&|(]\s*|\buv\s+run\s+(?:-\S+\s+)*|\bpython[\d.]*\s+-m\s+|\btimeout\s+\S+\s+|\b\w+=\S*\s+|/)"
_CHECKS = re.compile(rf"{_CMD}(?:ruff|mypy|pytest|py\.test|alembic|lint-imports)(?:$|[\s;&|)])")
_SERVICE = re.compile(
    rf"{_CMD}uvicorn(?:$|\s)|-m\s+tracksvc\b"
    r"|curl\b[^|;&]*(?:localhost|127\.0\.0\.1|0\.0\.0\.0):8000"
)
_RM = re.compile(r"(?:^|[;&|(]\s*|\s)rm\s+(?:-[A-Za-z]+\s+)*(?P<targets>[^;&|]+)")
# Writing a file by a shell redirect: `cat > f`, `cat <<EOF > f`, `cat > f <<EOF`, `tee f`, `printf … > f`.
_HEREDOC = r"<<-?\s*['\"]?\w+['\"]?"
_TARGET = r"(?P<append>>>?)\s*(?P<path>(?!/dev/|&)[^\s;&|<>]+)"
_SHELL_FILE_WRITE = re.compile(
    rf"(?:^|[\s;&|(])cat\s*(?:{_HEREDOC}\s*)?{_TARGET}"
    rf"|(?:^|[\s;&|(])printf\s[^;&|]*?{_TARGET.replace('append', 'append2').replace('path', 'path2')}"
    r"|\|\s*tee\s+(?P<tee_append>-a\s+)?(?P<path3>(?!/dev/)[^\s;&|<>]+)"
)
_SHELL_WRITE = re.compile(r"\bsed\s+-i|\bmkdir\b|\btouch\b|\becho\b[^;&|]*>>?\s*(?!/dev/|&)\S+")


# A file written from Python: open("path", "w" | "a" | "x") or Path("path").write_text / write_bytes.
_PY_FILE_WRITE = re.compile(
    r"""open\(\s*[rf]?(?P<q>['"])(?P<path>[^'"]+)(?P=q)\s*,\s*(?:mode\s*=\s*)?['"](?P<mode>[wax])[bt+]*['"]"""
    r"""|Path\(\s*[rf]?(?P<q2>['"])(?P<path2>[^'"]+)(?P=q2)\s*\)\s*\.write_(?:text|bytes)\("""
)
_PY_SHELL_LINE = re.compile(r"(?m)^\s*!(?P<cmd>.+)$")


def _classify_cell(code: str, written: set[str]) -> str:
    """Category of a Python cell (see the module docstring)."""
    writes = list(_PY_FILE_WRITE.finditer(code))
    if writes:
        again = False
        for match in writes:
            path = match.group("path") or match.group("path2")
            again = again or (match.group("mode") != "a" and path in written)
            written.add(path)
        return "rm_recreate" if again else "file_edit"
    shell = [m.group("cmd") for m in _PY_SHELL_LINE.finditer(code)]
    if code.lstrip().startswith("%%bash"):
        shell.append(code.split("\n", 1)[1] if "\n" in code else "")
    if shell:
        return classify("execute_bash", {"command": "\n".join(shell)}, written)
    return "library_read" if _library(code) else "experiment"


def _args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def tool_calls(exchange: dict[str, Any]) -> list[tuple[str, dict[str, Any], bool]]:
    """(tool name, arguments, arguments parsed) of the answer in a logged exchange."""
    assembled = exchange.get("response_assembled")
    if assembled is not None:
        raw = [(c.get("name") or "", c.get("arguments")) for c in assembled.get("tool_calls") or []]
    else:
        body = exchange.get("response_body")
        choices = body.get("choices") if isinstance(body, dict) else None
        message = (choices[0].get("message") or {}) if choices else {}
        raw = [
            ((c.get("function") or {}).get("name") or "", (c.get("function") or {}).get("arguments"))
            for c in message.get("tool_calls") or []
        ]
    calls = []
    for name, arguments in raw:
        parsed = _args(arguments)
        ok = isinstance(arguments, dict) or bool(parsed) or (isinstance(arguments, str) and arguments.strip() in ("", "{}"))
        calls.append((name, parsed, ok))
    return calls


def _library(text: str) -> bool:
    return bool(_LIB_PATH.search(text) or _LIB_INTROSPECTION.search(text))


def library_roots(text: str) -> list[str]:
    """Environment roots of the library paths in a command: `/opt/openhands-venv`, `/app/.venv`, …

    A relative root is taken as relative to /app, the agent's working directory.
    """
    roots = []
    for match in _VENV_ROOT.finditer(text):
        if not _LIB_PATH.match(text, match.end() - len("site-packages/")):
            continue
        root = match.group("root")
        if not root.startswith("/"):
            root = "/app/" + root.removeprefix("./")
        roots.append(root)
    return roots


def classify(name: str, args: dict[str, Any], written: set[str]) -> str:
    """Category of one tool call. `written` holds the files written so far in the step."""
    if name in SHELL_TOOLS:
        command = str(args.get("command") or "")
        writes = list(_SHELL_FILE_WRITE.finditer(command))
        if writes:
            removed = _RM.search(command[: writes[0].start()])
            again = bool(removed)
            for match in writes:
                path = match.group("path") or match.group("path2") or match.group("path3")
                whole = (match.group("append") or match.group("append2") or "") == ">" or (
                    match.group("path3") is not None and not match.group("tee_append")
                )
                again = again or (whole and path in written)
                written.add(path)
            return "rm_recreate" if again else "file_edit"
        if _library(command):
            return "library_read"
        if _SERVICE.search(command):
            return "service_run"
        if _CHECKS.search(command):
            return "checks"
        if _EXPERIMENT.search(command):
            return "experiment"
        removed = _RM.search(command)
        if removed:
            for target in removed.group("targets").split():
                written.discard(target)
            return "rm_recreate"
        if _SHELL_WRITE.search(command):
            return "file_edit"
        return "other"
    if name in PYTHON_CELL_TOOLS:
        return _classify_cell(str(args.get("code") or ""), written)
    if name in EDITOR_TOOLS:
        key = EDITOR_TOOLS[name]
        action = str(args.get(key) or "") if key else name
        path = next((str(args[k]) for k in PATH_KEYS if args.get(k)), "")
        if action in ("view", "read"):
            return "library_read" if _library(path + "/") else "other"
        if action in ("create", "write"):
            again = path in written
            written.add(path)
            return "rm_recreate" if again else "file_edit"
        if action in ("str_replace", "insert", "edit", "undo_edit"):
            written.add(path)
            return "file_edit"
    return "other"


_TOOL_FAILED = re.compile(r"exit code (?!0\b)-?\d+|\[An error occurred during execution\.\]|\A\s*(?:ERROR\b|Error:)")


# Last line of the summarizing prompt of OpenHands SDK's LLMSummarizingCondenser.
_CONDENSER_PROMPT = "Now summarize the events using the rules above."
_CONTEXT_OVERFLOW = re.compile(r"maximum context length", re.I)
_TEMPLATE_REJECTED = re.compile(r"last message is from the assistant", re.I)


def _error_message(exchange: dict[str, Any]) -> str:
    body = exchange.get("response_body")
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or body or exchange.get("response_stream_raw") or "")


def _output_limit(entry: dict[str, Any], exchange: dict[str, Any]) -> int | None:
    """Output limit of a request: sent by the harness or added by the proxy."""
    for source in (exchange.get("request_body"), exchange.get("proxy_injected"), entry.get("other_fields"), entry.get("proxy_injected")):
        if isinstance(source, dict):
            for key in ("max_completion_tokens", "max_tokens"):
                if isinstance(source.get(key), int):
                    return int(source[key])
    return None


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def _new_tool_results(messages: Any, seen: set[str]) -> list[tuple[str, bool]]:
    """(tool name, succeeded) for tool results not seen in earlier requests."""
    names: dict[str, str] = {}
    results = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            names[str(call.get("id"))] = (call.get("function") or {}).get("name") or ""
        call_id = message.get("tool_call_id")
        if message.get("role") == "tool" and call_id is not None and str(call_id) not in seen:
            seen.add(str(call_id))
            name = names.get(str(call_id)) or str(message.get("name") or "")
            results.append((name, not _TOOL_FAILED.search(_text(message.get("content")))))
    return results


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def summarize(traffic_dir: Path, step: str, step_started_at: str | None = None) -> dict[str, Any] | None:
    """Per-category turns, tool calls, completion tokens and seconds of one step.

    `step_started_at`: when the agent of the step started (proxy state); the
    first request of the step when not given.
    """
    index = traffic_dir / "index.jsonl"
    if not index.is_file():
        return None
    entries = []
    for line in index.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("step") == step and entry.get("method") == "POST":
            entries.append(entry)
    totals = {c: {"turns": 0.0, "tool_calls": 0, "completion_tokens": 0.0, "model_sec": 0.0, "tools_sec": 0.0} for c in CATEGORIES}
    roots: dict[str, int] = {}
    written: set[str] = set()
    turns = calls_total = unparsed = unread = 0
    longest = {"completion_tokens": 0, "model_sec": 0.0}
    last_turn: dict[str, Any] | None = None
    seen_results: set[str] = set()
    results = {"returned": 0, "failed": 0}
    last_ok: dict[str, Any] | None = None
    http_errors: dict[str, Any] = {
        "context_overflow": 0, "last_context_overflow_seq": None, "other": 0, "template_rejected": 0, "other_examples": [],
    }
    condensations = {"requests": 0, "answered": 0, "completion_tokens": 0}
    limit_hits: dict[str, Any] = {"count": 0, "completion_tokens": 0, "finish_reasons": {}, "without_tool_call": 0}
    invalid_names: dict[str, Any] = {"count": 0, "examples": []}
    origin = _parse_time(step_started_at) or (_parse_time(entries[0].get("started_at")) if entries else None)
    for position, entry in enumerate(entries):
        try:
            exchange = json.loads((traffic_dir / entry["file"]).read_text())
        except (OSError, json.JSONDecodeError, KeyError):
            unread += 1
            continue
        # Tool results the harness reports with this request (any request, also a failed one).
        for tool, succeeded in _new_tool_results((exchange.get("request_body") or {}).get("messages"), seen_results):
            results["returned"] += 1
            results["failed"] += not succeeded
            if succeeded:
                at = _parse_time(entry.get("started_at"))
                last_ok = {
                    "tool": tool,
                    "result_sent_at": entry.get("started_at"),
                    "sec_from_step_start": round((at - origin).total_seconds(), 1) if at and origin else None,
                }
        status = entry.get("status")
        messages = (exchange.get("request_body") or {}).get("messages")
        if isinstance(messages, list) and messages and _CONDENSER_PROMPT in _text((messages[-1] or {}).get("content")):
            condensations["requests"] += 1
            condensations["answered"] += status == 200
            condensations["completion_tokens"] += int((entry.get("usage") or {}).get("completion_tokens") or 0)
        if isinstance(status, int) and 400 <= status < 500:
            message = _error_message(exchange)
            if status == 400 and _CONTEXT_OVERFLOW.search(message):
                http_errors["context_overflow"] += 1
                http_errors["last_context_overflow_seq"] = entry.get("seq")
            else:
                http_errors["other"] += 1
                last_role = (messages[-1] or {}).get("role") if isinstance(messages, list) and messages else None
                if status == 400 and _TEMPLATE_REJECTED.search(message) and last_role == "assistant":
                    http_errors["template_rejected"] += 1
                if len(http_errors["other_examples"]) < 5:
                    http_errors["other_examples"].append({"status": status, "message": message[:300]})
        # A turn: an answered generation request with tools (as counted by the proxy).
        if entry.get("auxiliary") or status != 200 or "usage" not in entry:
            continue
        turns += 1
        calls = tool_calls(exchange)
        tokens = float((entry.get("usage") or {}).get("completion_tokens") or 0)
        limit = _output_limit(entry, exchange)
        if limit and tokens >= limit:
            reason = str((entry.get("response") or {}).get("finish_reason"))
            limit_hits["count"] += 1
            limit_hits["completion_tokens"] += int(tokens)
            limit_hits["finish_reasons"][reason] = limit_hits["finish_reasons"].get(reason, 0) + 1
            limit_hits["without_tool_call"] += not calls
        offered = {
            (tool.get("function") or {}).get("name")
            for tool in (exchange.get("request_body") or {}).get("tools") or []
            if isinstance(tool, dict)
        }
        for name, _args_, _parsed in calls:
            if offered and name not in offered:
                invalid_names["count"] += 1
                if len(invalid_names["examples"]) < 5:
                    invalid_names["examples"].append({"seq": entry.get("seq"), "name": name[:120], "name_chars": len(name)})
        model_sec = float(entry.get("elapsed_sec") or 0)
        names = [name for name, _a, _p in calls]
        reason = (entry.get("response") or {}).get("finish_reason")
        last_turn = {
            "seq": entry.get("seq"),
            "tools": [name[:60] for name in names],
            "finish_reason": reason,
            "final_answer": "finish" in names or (not names and reason == "stop"),
        }
        if tokens > longest["completion_tokens"]:
            longest = {"completion_tokens": int(tokens), "model_sec": round(model_sec, 1)}
        finished = _parse_time(exchange.get("finished_at"))
        following = _parse_time(entries[position + 1].get("started_at")) if position + 1 < len(entries) else None
        tools_sec = max((following - finished).total_seconds(), 0.0) if finished and following else 0.0
        labels = []
        for name, args, parsed in calls:
            unparsed += not parsed
            label = classify(name, args, written)
            labels.append(label)
            if label == "library_read":
                text = str(args.get("command") or "") if name in SHELL_TOOLS else next((str(args[k]) for k in PATH_KEYS if args.get(k)), "")
                for root in set(library_roots(text)):
                    roots[root] = roots.get(root, 0) + 1
        calls_total += len(labels)
        for label in labels or ["other"]:
            share = 1 / max(len(labels), 1)
            bucket = totals[label]
            bucket["turns"] += share
            bucket["tool_calls"] += 1 if labels else 0
            bucket["completion_tokens"] += tokens * share
            bucket["model_sec"] += model_sec * share
            bucket["tools_sec"] += tools_sec * share
    all_tokens = sum(b["completion_tokens"] for b in totals.values())
    categories = {
        name: {
            "turns": round(bucket["turns"], 2),
            "tool_calls": bucket["tool_calls"],
            "completion_tokens": round(bucket["completion_tokens"]),
            "token_share": round(bucket["completion_tokens"] / all_tokens, 3) if all_tokens else None,
            "model_sec": round(bucket["model_sec"], 1),
            "tools_sec": round(bucket["tools_sec"], 1),
        }
        for name, bucket in totals.items()
    }
    return {
        "turns": turns,
        "tool_calls": calls_total,
        "tool_calls_unparsed_arguments": unparsed,
        "exchanges_unreadable": unread,
        "longest_response": longest,
        "last_turn": last_turn,
        # Tool results the harness sent back to the model; calls without a result are in neither.
        "tool_results": results,
        "last_successful_tool_call": last_ok,
        "http_errors": http_errors,
        "output_limit_hits": limit_hits,
        "invalid_tool_names": invalid_names,
        # Summarizing requests of the OpenHands SDK condenser (not turns: no tools).
        "condensations": condensations,
        "categories": categories,
        # Which environment the library sources were read from (tool calls, not turns).
        "library_read_roots": dict(sorted(roots.items())),
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    summary = summarize(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "step-1")
    print(json.dumps(summary, indent=2))
    return 0 if summary else 1


if __name__ == "__main__":
    sys.exit(main())
