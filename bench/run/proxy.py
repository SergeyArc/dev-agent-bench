#!/usr/bin/env python3
"""Recording and budget-enforcing proxy between a harness and vLLM.

Runs on the host, outside the task container. Containers talk to this proxy
(``http://host.docker.internal:<port>/v1``); the proxy talks to the vLLM upstream.

What it does:

* swaps the per-trial client key for the real vLLM key (the real key never
  enters the container, so the agent cannot bypass the proxy);
* records every exchange per step: request as sent by the harness, response
  including the raw SSE stream, ``usage``;
* enforces a completion-token budget per step (reasoning tokens included, as
  vLLM counts them in ``completion_tokens``); once exhausted, further
  generation requests get HTTP 403 ``budget_exhausted``;
* adds ``max_tokens`` to generation requests that carry no output limit and
  removes ``reasoning_effort`` (thinking is configured on the server);
* serves only the paths the harnesses use (``ALLOWED_PATHS``), 404 otherwise;
* learns step boundaries from the runner over ``/_control/*`` (guarded by a
  control token that the container never sees).

Standard library only, on purpose: nothing to pin, nothing to install.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import select
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SAMPLING_FIELDS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "frequency_penalty",
    "repetition_penalty",
    "seed",
)
# Values that leave the distribution untouched (vLLM defaults).
NEUTRAL_SAMPLING = {
    "top_p": 1.0,
    "min_p": 0.0,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
    "repetition_penalty": 1.0,
}
# Request fields worth a column in index.jsonl besides sampling.
OTHER_FIELDS = (
    "max_tokens",
    "max_completion_tokens",
    "chat_template_kwargs",
    "reasoning_effort",
    "tool_choice",
    "parallel_tool_calls",
    "stream",
    "stream_options",
    "stop",
    "n",
    "extra_body",
)
GENERATION_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/responses")
# Everything the three harnesses and the runner are known to send; any other
# path gets 404 from the proxy and never reaches vLLM.
ALLOWED_PATHS = {("GET", "/v1/models"), ("POST", "/v1/chat/completions")}
# Request fields removed before forwarding: thinking is configured on the server.
STRIPPED_FIELDS = ("reasoning_effort",)
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "authorization",
    "accept-encoding",
}
NO_STEP = "_outside-step"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StepState:
    """Counters of one bench step."""

    def __init__(self, name: str, budget: int | None) -> None:
        self.name = name
        self.budget = budget
        self.started_at = now_iso()
        self.ended_at: str | None = None
        self.requests = 0
        # Model responses to requests that carry tools: the agent's own turns.
        self.turns = 0
        # Requests without tools (opencode's session title, summaries): their
        # tokens count towards the budget, but they are not turns.
        self.auxiliary_requests = 0
        self.auxiliary_turns = 0
        self.auxiliary_completion_tokens = 0
        self.length_finishes = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reasoning_tokens = 0
        self.reasoning_tokens_reported = False
        self.estimated_completion_tokens = 0
        self.peak_context_tokens = 0
        self.refused_budget = 0
        self.upstream_errors = 0
        # A request repeating the body of the request that just failed upstream.
        self.upstream_retries = 0
        # True while the latest generation request failed upstream and nothing
        # succeeded after it: the step stopped because of the infrastructure.
        self.ended_on_upstream_error = False
        self.last_failed_request_sha256: str | None = None
        # Times the proxy waited for an unreachable upstream to come back before
        # a response had started (upstream drop), and how long it waited in all.
        self.upstream_waits = 0
        self.upstream_wait_seconds = 0.0
        self.http_errors = 0
        self.aborted_streams = 0
        # Responses whose completion tokens are a chunk-count estimate (no usage).
        self.usage_estimated = 0
        # Responses the client abandoned before they ended (it disconnected),
        # and the completion tokens they still cost.
        self.client_aborted = 0
        self.client_aborted_completion_tokens = 0
        # Read timeout the HTTP client announced (x-stainless-read-timeout), seconds.
        self.client_timeouts_seen: list[str] = []
        self.inflight = 0
        self.max_inflight = 0
        self.rejected_paths = 0
        self.rejected_path_list: list[str] = []
        self.stripped_fields: dict[str, list[Any]] = {}
        self.max_response_completion_tokens = 0
        # A request carrying fewer messages than the previous one: the harness
        # compacted its history.
        self.history_shrinks = 0
        self.last_n_messages = 0
        self.budget_exhausted = False
        self.sampling_fields_sent: dict[str, list[Any]] = {}
        self.sampling_violations = 0
        self.chat_template_kwargs_sent: list[Any] = []
        self.models_requested: list[str] = []
        self.last_response: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


class ProxyState:
    """Shared state: current step, counters, traffic log location."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.lock = threading.Lock()
        self.traffic_dir = Path(args.traffic_dir)
        self.traffic_dir.mkdir(parents=True, exist_ok=True)
        upstream = urlsplit(args.upstream)
        if upstream.scheme != "http" or not upstream.hostname:
            raise SystemExit(f"unsupported upstream URL: {args.upstream}")
        self.upstream_host = upstream.hostname
        self.upstream_port = upstream.port or 80
        self.upstream_timeout = args.upstream_timeout
        self.upstream_wait_max = args.upstream_wait
        self.upstream_poll = args.upstream_poll
        self.upstream_key = os.environ.get("BENCH_PROXY_UPSTREAM_KEY", "")
        self.client_key = os.environ["BENCH_PROXY_CLIENT_KEY"]
        self.control_token = os.environ["BENCH_PROXY_CONTROL_TOKEN"]
        self.default_budget = args.budget
        self.inject_max_tokens = args.inject_max_tokens
        expected = os.environ.get("BENCH_PROXY_EXPECTED_SAMPLING", "")
        self.expected_sampling: dict[str, Any] | None = json.loads(expected) if expected else None
        self.steps: dict[str, StepState] = {}
        self.current: StepState | None = None
        self.outside = StepState(NO_STEP, 0)
        self.seq = 0
        # Set by the runner when the pre-start probe reached vLLM around the
        # proxy: nothing is served any more.
        self.locked = False

    def begin_step(self, name: str, budget: int | None) -> StepState:
        with self.lock:
            if self.current is not None and self.current.ended_at is None:
                self.current.ended_at = now_iso()
            # A fresh counter per step; a repeated name (should not happen)
            # gets a suffix instead of silently merging two sessions.
            key = name
            n = 2
            while key in self.steps:
                key = f"{name}#{n}"
                n += 1
            step = StepState(key, self.default_budget if budget is None else budget)
            self.steps[key] = step
            self.current = step
            self._dump_locked()
            return step

    def end_step(self) -> None:
        with self.lock:
            if self.current is not None:
                self.current.ended_at = now_iso()
            self.current = None
            self._dump_locked()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "written_at": now_iso(),
            "current_step": self.current.name if self.current else None,
            "default_budget": self.default_budget,
            "locked": self.locked,
            "expected_sampling": self.expected_sampling,
            "steps": {name: step.to_dict() for name, step in self.steps.items()},
            "outside_step": self.outside.to_dict(),
        }

    def _dump_locked(self) -> None:
        tmp = self.traffic_dir / "state.json.tmp"
        tmp.write_text(json.dumps(self._snapshot_locked(), indent=2, ensure_ascii=False))
        tmp.replace(self.traffic_dir / "state.json")

    def dump(self) -> None:
        with self.lock:
            self._dump_locked()

    def next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq


class StreamSummary:
    """Incremental reading of an OpenAI-style SSE stream."""

    def __init__(self) -> None:
        self.usage: dict[str, Any] | None = None
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.reasoning_field: str | None = None
        self.tool_calls: dict[int, dict[str, str]] = {}
        self.finish_reason: str | None = None
        self.delta_chunks = 0
        self.done = False

    def feed(self, payload: str) -> bool:
        """Consume one ``data:`` payload. Returns True for a usage-only chunk."""
        if payload == "[DONE]":
            self.done = True
            return False
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            return False
        if not isinstance(chunk, dict):
            return False
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        for choice in choices:
            delta = choice.get("delta") or {}
            produced = False
            if delta.get("content"):
                self.content.append(delta["content"])
                produced = True
            for field in ("reasoning", "reasoning_content"):
                if delta.get(field):
                    self.reasoning.append(delta[field])
                    self.reasoning_field = field
                    produced = True
            for call in delta.get("tool_calls") or []:
                slot = self.tool_calls.setdefault(call.get("index", 0), {"name": "", "arguments": ""})
                function = call.get("function") or {}
                slot["name"] += function.get("name") or ""
                slot["arguments"] += function.get("arguments") or ""
                produced = True
            if choice.get("text"):
                self.content.append(choice["text"])
                produced = True
            if produced:
                self.delta_chunks += 1
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        return not choices and "usage" in chunk

    def result(self) -> dict[str, Any]:
        return {
            "content": "".join(self.content),
            "reasoning": "".join(self.reasoning),
            "reasoning_field": self.reasoning_field,
            "tool_calls": [self.tool_calls[i] for i in sorted(self.tool_calls)],
            "finish_reason": self.finish_reason,
        }


def summarize_message(body: dict[str, Any]) -> dict[str, Any]:
    """Same shape as ``StreamSummary.result`` for a non-streaming response."""
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    reasoning_field = next((f for f in ("reasoning", "reasoning_content") if message.get(f)), None)
    return {
        "content": message.get("content") or choice.get("text") or "",
        "reasoning": message.get(reasoning_field) if reasoning_field else "",
        "reasoning_field": reasoning_field,
        "tool_calls": [
            {
                "name": (c.get("function") or {}).get("name", ""),
                "arguments": (c.get("function") or {}).get("arguments", ""),
            }
            for c in message.get("tool_calls") or []
        ],
        "finish_reason": choice.get("finish_reason"),
    }


def history_reasoning_fields(messages: Any) -> dict[str, int]:
    """Which reasoning fields the harness returns in assistant history turns."""
    counts = {"assistant_messages": 0, "reasoning": 0, "reasoning_content": 0, "think_tag_in_content": 0}
    if not isinstance(messages, list):
        return counts
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        counts["assistant_messages"] += 1
        for field in ("reasoning", "reasoning_content"):
            if message.get(field):
                counts[field] += 1
        content = message.get("content")
        if isinstance(content, str) and "<think>" in content:
            counts["think_tag_in_content"] += 1
    return counts


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: ProxyState

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    # -- helpers ---------------------------------------------------------

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str, message: str) -> dict[str, Any]:
        payload = {"error": {"message": message, "type": code, "code": code, "param": None}}
        self._send_json(status, payload)
        return payload

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _client_gone(self) -> bool:
        """True when the client has closed its side of the connection."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _bearer(self) -> str:
        value = self.headers.get("Authorization") or ""
        return value[7:].strip() if value.lower().startswith("bearer ") else ""

    # -- control ---------------------------------------------------------

    def _control(self, method: str, body: bytes) -> None:
        if self.headers.get("X-Bench-Control") != self.state.control_token:
            self._error(403, "forbidden", "control token required")
            return
        path = urlsplit(self.path).path
        if method == "GET" and path == "/_control/state":
            self._send_json(200, self.state.snapshot())
        elif method == "POST" and path == "/_control/step":
            data = json.loads(body or b"{}")
            step = self.state.begin_step(str(data["step"]), data.get("budget"))
            self._send_json(200, {"step": step.name, "budget": step.budget})
        elif method == "POST" and path == "/_control/step-end":
            self.state.end_step()
            self._send_json(200, {"ok": True})
        elif method == "POST" and path == "/_control/lockdown":
            self.state.locked = True
            self.state.dump()
            self._send_json(200, {"locked": True})
        else:
            self._error(404, "not_found", "unknown control endpoint")

    # -- proxying --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        body = self._read_body()
        path = urlsplit(self.path).path
        if path.startswith("/_control/"):
            self._control(method, body)
            return
        if path == "/_proxy/health":
            self._send_json(200, {"ok": True})
            return
        if self._bearer() != self.state.client_key:
            self._error(401, "invalid_api_key", "invalid API key")
            return
        state = self.state
        if state.locked:
            self._error(403, "trial_locked", "bench proxy: the trial is locked by the runner")
            return
        step = state.current if state.current is not None else state.outside
        if (method, path) not in ALLOWED_PATHS:
            with state.lock:
                step.rejected_paths += 1
                entry = f"{method} {path}"
                if entry not in step.rejected_path_list and len(step.rejected_path_list) < 50:
                    step.rejected_path_list.append(entry)
                state._dump_locked()
            self._error(404, "path_not_allowed", f"bench proxy: {method} {path} is not served")
            return
        self._proxy(method, path, body, step)

    def _proxy(self, method: str, path: str, body: bytes, step: StepState) -> None:
        state = self.state
        is_generation = method == "POST" and path in GENERATION_PATHS
        seq = state.next_seq()
        started = time.monotonic()
        record: dict[str, Any] = {
            "seq": seq,
            "step": step.name,
            "started_at": now_iso(),
            "method": method,
            "path": self.path,
            "request_headers": {
                k: ("<redacted>" if k.lower() == "authorization" else v) for k, v in self.headers.items()
            },
        }
        request_json: dict[str, Any] | None = None
        if body:
            try:
                parsed = json.loads(body)
                request_json = parsed if isinstance(parsed, dict) else None
                record["request_body"] = parsed
            except json.JSONDecodeError:
                record["request_body_raw"] = body.decode("utf-8", "replace")

        index: dict[str, Any] = {
            "seq": seq,
            "step": step.name,
            "started_at": record["started_at"],
            "method": method,
            "path": path,
        }
        forward_body = body
        injected_usage = False
        streaming = False
        auxiliary = False
        body_sha: str | None = None
        if is_generation and request_json is not None:
            sampling = {k: request_json[k] for k in SAMPLING_FIELDS if k in request_json}
            other = {k: request_json[k] for k in OTHER_FIELDS if k in request_json}
            violations = self._sampling_violations(sampling)
            index.update(
                model=request_json.get("model"),
                sampling_sent=sampling,
                sampling_violations=violations,
                other_fields=other,
                n_messages=len(request_json.get("messages") or []),
                n_tools=len(request_json.get("tools") or []),
                history_reasoning=history_reasoning_fields(request_json.get("messages")),
            )
            streaming = bool(request_json.get("stream"))
            auxiliary = not request_json.get("tools")
            n_messages = len(request_json.get("messages") or [])
            body_sha = hashlib.sha256(body).hexdigest()
            index["auxiliary"] = auxiliary
            announced = self.headers.get("x-stainless-read-timeout")
            with state.lock:
                step.requests += 1
                if announced and announced not in step.client_timeouts_seen:
                    step.client_timeouts_seen.append(announced)
                if auxiliary:
                    step.auxiliary_requests += 1
                if not auxiliary:
                    if n_messages < step.last_n_messages:
                        step.history_shrinks += 1
                        index["history_shrink"] = {"from": step.last_n_messages, "to": n_messages}
                    step.last_n_messages = n_messages
                if step.last_failed_request_sha256 == body_sha:
                    step.upstream_retries += 1
                    index["upstream_retry"] = True
                for key, value in sampling.items():
                    seen = step.sampling_fields_sent.setdefault(key, [])
                    if value not in seen:
                        seen.append(value)
                if violations:
                    step.sampling_violations += 1
                kwargs = request_json.get("chat_template_kwargs")
                if kwargs is not None and kwargs not in step.chat_template_kwargs_sent:
                    step.chat_template_kwargs_sent.append(kwargs)
                model = request_json.get("model")
                if isinstance(model, str) and model not in step.models_requested:
                    step.models_requested.append(model)
                refuse = step is state.outside or (
                    step.budget is not None and step.completion_tokens >= step.budget
                )
                if refuse and step is not state.outside:
                    step.budget_exhausted = True
                    step.refused_budget += 1
            if refuse:
                if step is state.outside:
                    payload = self._error(403, "no_active_step", "bench proxy: no active step")
                else:
                    payload = self._error(
                        403,
                        "budget_exhausted",
                        "bench proxy: the token budget of this step is exhausted; stop working",
                    )
                record.update(response_status=403, response_body=payload, refused=payload["error"]["code"])
                index.update(status=403, refused=payload["error"]["code"])
                self._finish(record, index, started, step)
                return
            patched = dict(request_json)
            injected: dict[str, Any] = {}
            # Usage of a stream is known only with include_usage; ask for it
            # ourselves and hide the extra chunk if the harness did not ask.
            if streaming:
                options = request_json.get("stream_options")
                if not (isinstance(options, dict) and options.get("include_usage")):
                    patched["stream_options"] = {**(options or {}), "include_usage": True}
                    injected_usage = True
                    injected["stream_options.include_usage"] = True
            # Same output limit for every harness: add one only when the harness
            # sent none; a limit the harness sent is never touched.
            limit = state.inject_max_tokens
            if (
                limit
                and path != "/v1/responses"
                and request_json.get("max_tokens") is None
                and request_json.get("max_completion_tokens") is None
            ):
                patched["max_tokens"] = limit
                injected["max_tokens"] = limit
            stripped = {k: patched.pop(k) for k in STRIPPED_FIELDS if k in patched}
            if stripped:
                record["proxy_stripped"] = stripped
                index["proxy_stripped"] = stripped
                with state.lock:
                    for key, value in stripped.items():
                        seen = step.stripped_fields.setdefault(key, [])
                        if value not in seen:
                            seen.append(value)
            if injected:
                record["proxy_injected"] = injected
                index["proxy_injected"] = injected
            if injected or stripped:
                forward_body = json.dumps(patched, ensure_ascii=False).encode()

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        if state.upstream_key:
            headers["Authorization"] = f"Bearer {state.upstream_key}"
        headers["Accept-Encoding"] = "identity"
        if forward_body:
            headers["Content-Length"] = str(len(forward_body))

        conn = http.client.HTTPConnection(
            state.upstream_host, state.upstream_port, timeout=state.upstream_timeout
        )
        # In flight = being generated upstream at the same time; each of them may
        # finish after the budget is spent, which bounds the overshoot.
        with state.lock:
            step.inflight += 1
            step.max_inflight = max(step.max_inflight, step.inflight)
        # The upstream may be unreachable for a while (the connection drops and
        # comes back). Nothing of a response has reached the client yet, so the
        # same request is sent again once /v1/models answers; the wait is bounded
        # per request. A break in the middle of a stream is never retried.
        waits: list[dict[str, Any]] = []
        wait_left = state.upstream_wait_max
        while True:
            try:
                conn.request(method, self.path, body=forward_body or None, headers=headers)
                response = conn.getresponse()
                break
            except OSError as exc:
                conn.close()
                waited, answered = self._wait_for_upstream(wait_left) if wait_left > 0 else (0.0, False)
                if waited > 0 or answered:
                    # A wait that ended without an answer is lost time of the step all the same.
                    wait = {"error": repr(exc), "waited_sec": round(waited, 1)}
                    if not answered:
                        wait["gave_up"] = True
                    waits.append(wait)
                    with state.lock:
                        step.upstream_waits += 1
                        step.upstream_wait_seconds = round(step.upstream_wait_seconds + waited, 1)
                if answered:
                    wait_left -= waited
                    conn = http.client.HTTPConnection(
                        state.upstream_host, state.upstream_port, timeout=state.upstream_timeout
                    )
                    continue
                with state.lock:
                    step.inflight -= 1
                self._upstream_failed(step, body_sha if is_generation else None)
                payload = self._error(502, "upstream_unreachable", f"bench proxy: upstream error: {exc!r}")
                record.update(response_status=502, response_body=payload, upstream_error=repr(exc))
                index.update(status=502, upstream_error=repr(exc))
                if waits:
                    record["upstream_waits"] = index["upstream_waits"] = waits
                self._finish(record, index, started, step)
                return
        if waits:
            record["upstream_waits"] = index["upstream_waits"] = waits

        try:
            record["response_status"] = response.status
            record["response_headers"] = dict(response.getheaders())
            index["status"] = response.status
            content_type = response.getheader("Content-Type") or ""
            if "text/event-stream" in content_type:
                broken = self._relay_stream(response, record, index, step, injected_usage, auxiliary)
                if broken:
                    self._upstream_failed(step, body_sha)
            else:
                self._relay_plain(response, record, index, step, is_generation, auxiliary)
        finally:
            conn.close()
            with state.lock:
                step.inflight -= 1
        if response.status >= 500:
            self._upstream_failed(step, body_sha if is_generation else None)
        elif response.status >= 400:
            with state.lock:
                step.http_errors += 1
        self._finish(record, index, started, step)

    def _wait_for_upstream(self, max_wait: float) -> tuple[float, bool]:
        """Poll the upstream's /v1/models until it answers with any HTTP status.

        Returns the seconds waited and whether it answered: False when it did
        not answer within `max_wait` or the client went away meanwhile.
        """
        state = self.state
        started = time.monotonic()
        while True:
            if self._client_gone():
                return time.monotonic() - started, False
            probe = http.client.HTTPConnection(state.upstream_host, state.upstream_port, timeout=10)
            try:
                probe.request("GET", "/v1/models", headers={"Authorization": f"Bearer {state.upstream_key}"})
                probe.getresponse().read()
                return time.monotonic() - started, True
            except OSError:
                pass
            finally:
                probe.close()
            remaining = max_wait - (time.monotonic() - started)
            if remaining <= 0:
                return time.monotonic() - started, False
            time.sleep(min(state.upstream_poll, remaining))

    def _upstream_failed(self, step: StepState, body_sha: str | None) -> None:
        with self.state.lock:
            step.upstream_errors += 1
            if body_sha is not None:
                step.ended_on_upstream_error = True
                step.last_failed_request_sha256 = body_sha

    def _sampling_violations(self, sampling: dict[str, Any]) -> dict[str, Any]:
        expected = self.state.expected_sampling
        if expected is None:
            return {}
        bad: dict[str, Any] = {}
        for key, value in sampling.items():
            if key == "seed":
                continue
            if key in expected:
                if float(value) != float(expected[key]):
                    bad[key] = {"sent": value, "vendor": expected[key]}
            elif key not in NEUTRAL_SAMPLING or float(value) != NEUTRAL_SAMPLING[key]:
                # A field the vendor leaves alone may only be sent as a no-op.
                bad[key] = {"sent": value, "vendor": None}
        return bad

    def _relay_plain(
        self,
        response: http.client.HTTPResponse,
        record: dict[str, Any],
        index: dict[str, Any],
        step: StepState,
        is_generation: bool,
        auxiliary: bool,
    ) -> None:
        data = response.read()
        # A client that gave up while vLLM was still generating gets nothing,
        # but the tokens are spent: count them.
        client_gone = self._client_gone()
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            client_gone = True
        if client_gone:
            record["client_disconnected"] = True
            index["client_aborted"] = True
            self.close_connection = True
        try:
            parsed = json.loads(data)
            record["response_body"] = parsed
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None
            record["response_body_raw"] = data.decode("utf-8", "replace")
        if is_generation and response.status == 200 and isinstance(parsed, dict):
            self._account(step, index, parsed.get("usage"), summarize_message(parsed), 0, auxiliary, client_gone)

    def _relay_stream(
        self,
        response: http.client.HTTPResponse,
        record: dict[str, Any],
        index: dict[str, Any],
        step: StepState,
        injected_usage: bool,
        auxiliary: bool,
    ) -> bool:
        """Relay an SSE stream. Returns True when the upstream broke mid-stream."""
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        summary = StreamSummary()
        raw: list[str] = []
        event: list[bytes] = []
        hide_event = False
        client_gone = False
        upstream_broken = False
        try:
            while True:
                line = response.readline()
                if not line:
                    break
                raw.append(line.decode("utf-8", "replace"))
                event.append(line)
                stripped = line.strip()
                if stripped.startswith(b"data:"):
                    usage_only = summary.feed(stripped[5:].strip().decode("utf-8", "replace"))
                    hide_event = hide_event or (usage_only and injected_usage)
                if stripped:
                    continue
                # Blank line: the event is complete.
                if not client_gone and self._client_gone():
                    client_gone = True
                    break
                if not hide_event and not client_gone:
                    try:
                        self.wfile.write(b"".join(event))
                        self.wfile.flush()
                    except OSError:
                        # Keep nothing running upstream for a client that left.
                        client_gone = True
                        break
                event = []
                hide_event = False
        except (OSError, http.client.HTTPException) as exc:
            upstream_broken = True
            record["upstream_error"] = repr(exc)
        if event and not client_gone and not hide_event:
            try:
                self.wfile.write(b"".join(event))
                self.wfile.flush()
            except OSError:
                client_gone = True
        record["response_stream_raw"] = "".join(raw)
        record["response_assembled"] = summary.result()
        record["stream_complete"] = summary.done
        if client_gone:
            record["client_disconnected"] = True
            index["client_aborted"] = True
        if response.status == 200:
            if not summary.done:
                with self.state.lock:
                    step.aborted_streams += 1
            self._account(
                step, index, summary.usage, summary.result(), summary.delta_chunks, auxiliary, client_gone
            )
        return upstream_broken

    def _account(
        self,
        step: StepState,
        index: dict[str, Any],
        usage: dict[str, Any] | None,
        message: dict[str, Any],
        delta_chunks: int,
        auxiliary: bool,
        client_aborted: bool = False,
    ) -> None:
        tail = message["content"].strip()[-400:]
        last = {
            "seq": index["seq"],
            "content_tail": tail,
            "tool_calls": len(message["tool_calls"]),
            "finish_reason": message["finish_reason"],
            "reasoning_field": message["reasoning_field"],
            "reasoning_chars": len(message["reasoning"] or ""),
        }
        index.update(response=last)
        with self.state.lock:
            step.ended_on_upstream_error = False
            step.last_failed_request_sha256 = None
            if message["finish_reason"] == "length":
                step.length_finishes += 1
            if auxiliary:
                step.auxiliary_turns += 1
            else:
                step.turns += 1
                step.last_response = last
            if isinstance(usage, dict):
                prompt = int(usage.get("prompt_tokens") or 0)
                completion = int(usage.get("completion_tokens") or 0)
                details = usage.get("completion_tokens_details") or {}
                if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
                    step.reasoning_tokens += int(details["reasoning_tokens"])
                    step.reasoning_tokens_reported = True
                index["usage"] = usage
            else:
                # No usage (stream cut short): vLLM emits about one token per
                # delta chunk, so the chunk count is the best estimate we have.
                prompt = 0
                completion = delta_chunks
                step.estimated_completion_tokens += delta_chunks
                step.usage_estimated += 1
                index["usage_estimated"] = {"completion_tokens": delta_chunks}
            step.prompt_tokens += prompt
            step.completion_tokens += completion
            step.max_response_completion_tokens = max(step.max_response_completion_tokens, completion)
            if client_aborted:
                step.client_aborted += 1
                step.client_aborted_completion_tokens += completion
            if auxiliary:
                step.auxiliary_completion_tokens += completion
            step.peak_context_tokens = max(step.peak_context_tokens, prompt + completion)
            if step.budget is not None and step.completion_tokens >= step.budget:
                step.budget_exhausted = True

    def _finish(
        self, record: dict[str, Any], index: dict[str, Any], started: float, step: StepState
    ) -> None:
        elapsed = round(time.monotonic() - started, 3)
        record["finished_at"] = now_iso()
        record["elapsed_sec"] = elapsed
        index["elapsed_sec"] = elapsed
        step_dir = self.state.traffic_dir / step.name
        step_dir.mkdir(parents=True, exist_ok=True)
        name = f"{record['seq']:06d}.json"
        (step_dir / name).write_text(json.dumps(record, indent=1, ensure_ascii=False))
        index["file"] = f"{step.name}/{name}"
        with self.state.lock:
            with (self.state.traffic_dir / "index.jsonl").open("a") as fh:
                fh.write(json.dumps(index, ensure_ascii=False) + "\n")
            self.state._dump_locked()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8301)
    parser.add_argument("--upstream", default="http://127.0.0.1:8300")
    parser.add_argument("--traffic-dir", required=True)
    parser.add_argument("--budget", type=int, default=1_000_000, help="completion tokens per step")
    parser.add_argument(
        "--inject-max-tokens", type=int, default=0,
        help="max_tokens added to generation requests that carry no output limit (0 = off)",
    )
    parser.add_argument("--upstream-timeout", type=float, default=1800.0)
    parser.add_argument("--upstream-wait", type=float, default=900.0,
                        help="seconds to wait for an unreachable upstream before a request is given up (0 = do not wait)")
    parser.add_argument("--upstream-poll", type=float, default=10.0, help="seconds between polls of /v1/models while waiting")
    args = parser.parse_args()

    state = ProxyState(args)
    Handler.state = state
    server = ThreadingHTTPServer((args.listen_host, args.port), Handler)
    server.daemon_threads = True
    state.dump()
    print(f"bench proxy on {args.listen_host}:{args.port} -> {args.upstream}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.dump()
    return 0


if __name__ == "__main__":
    sys.exit(main())
