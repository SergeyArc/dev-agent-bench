#!/usr/bin/env python3
"""Tiny stand-in for vLLM, for testing the proxy and harness wiring without a GPU.

Behaviour of POST /v1/chat/completions:
* ``--probe CMD`` and a bash-like tool, no tool result yet -> that shell call;
* the request offers a ``finish`` tool  -> one tool call ``finish``;
* otherwise                              -> a short text answer.
Every answer carries a ``reasoning`` field, like vLLM v0.30.0. Streaming sends
``usage`` only when ``stream_options.include_usage`` is set, also like vLLM.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "mock-model"
MAX_MODEL_LEN = 65536
API_KEY = ""
COMPLETION_TOKENS = 40
TEXT = "All done."
PROBE = ""
PROBE2 = ""
DELAY_AT = 0  # 1-based number of the chat request answered after DELAY seconds
DELAY = 0.0
FAIL_ONCE_AT = 0  # 1-based number of the chat request answered with HTTP 500
LENGTH_AT = 0  # 1-based number of the chat request finished with "length"
OVERFLOW_AT = 0  # 1-based number of the chat request answered with HTTP 400 "maximum context length"
PREEMPTIONS = 0  # reported on /metrics; grows by PREEMPT_EVERY-th chat request
PREEMPT_EVERY = 0
CHAT_REQUESTS = 0
RECEIVED: list[dict[str, Any]] = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if not API_KEY or self.headers.get("Authorization") == f"Bearer {API_KEY}":
            return True
        self._json(401, {"error": "Unauthorized"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/_mock/requests":
            self._json(200, {"requests": RECEIVED})
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/metrics" and PREEMPTIONS >= 0:
            extra = CHAT_REQUESTS // PREEMPT_EVERY if PREEMPT_EVERY else 0
            text = (
                "# HELP vllm:num_preemptions_total Cumulative number of preemption from the engine.\n"
                "# TYPE vllm:num_preemptions_total counter\n"
                f'vllm:num_preemptions_total{{engine="0",model_name="{MODEL}"}} {float(PREEMPTIONS + extra)}\n'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)
        elif self.path == "/v1/models":
            if self._authorized():
                model = {"id": MODEL, "object": "model", "max_model_len": MAX_MODEL_LEN}
                self._json(200, {"object": "list", "data": [model]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not self._authorized():
            return
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return
        # What actually arrived upstream, without the bulky parts.
        RECEIVED.append({k: v for k, v in body.items() if k not in ("messages", "tools") and not k.startswith("mock_")})
        # Test hooks carried in the request body: mock_delay (s), mock_cut (stream ends early).
        time.sleep(float(body.get("mock_delay") or 0))
        global CHAT_REQUESTS
        CHAT_REQUESTS += 1
        if CHAT_REQUESTS == FAIL_ONCE_AT:
            self._json(500, {"error": {"message": "mock failure", "type": "server_error"}})
            return
        if CHAT_REQUESTS == OVERFLOW_AT:
            message = (
                f"This model's maximum context length is {MAX_MODEL_LEN} tokens. However, you requested 32000 output "
                f"tokens and your prompt contains at least {MAX_MODEL_LEN - 31999} input tokens, for a total of at least "
                f"{MAX_MODEL_LEN + 1} tokens. Please reduce the length of the input prompt or the number of requested "
                f"output tokens. (parameter=input_tokens, value={MAX_MODEL_LEN - 31999})"
            )
            self._json(400, {"error": {"message": message, "type": "BadRequestError", "param": "input_tokens", "code": 400}})
            return
        cut = CHAT_REQUESTS == LENGTH_AT
        specs = {t.get("function", {}).get("name"): t.get("function", {}) for t in body.get("tools") or []}
        tools = list(specs)
        tool_calls = None
        shell = next((name for name in tools if name and ("bash" in name or name == "terminal")), None)
        tool_results = sum(m.get("role") == "tool" for m in body.get("messages") or [])
        probe = PROBE if tool_results == 0 else PROBE2 if tool_results == 1 else ""
        if CHAT_REQUESTS == DELAY_AT:
            time.sleep(DELAY)
        if shell and probe:
            # One shell call first: shows who the agent is and what it sees.
            wanted = {"command": probe, "description": "probe", "security_risk": "LOW"}
            properties = (specs[shell].get("parameters") or {}).get("properties") or {}
            arguments = json.dumps({k: v for k, v in wanted.items() if k in properties})
            function = {"name": shell, "arguments": arguments}
            tool_calls = [{"id": f"call_mock_{CHAT_REQUESTS}", "type": "function", "function": function}]
        elif "finish" in tools:
            arguments = json.dumps({"message": TEXT})
            function = {"name": "finish", "arguments": arguments}
            tool_calls = [{"id": "call_mock_1", "type": "function", "function": function}]
        usage = {
            "prompt_tokens": 100 * len(body.get("messages") or []),
            "completion_tokens": COMPLETION_TOKENS,
            "total_tokens": 100 * len(body.get("messages") or []) + COMPLETION_TOKENS,
        }
        base = {"id": "chatcmpl-mock", "created": int(time.time()), "model": body.get("model")}
        if not body.get("stream"):
            message: dict[str, Any] = {"role": "assistant", "reasoning": "thinking", "content": None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            else:
                message["content"] = TEXT
            finish = "length" if cut else "tool_calls" if tool_calls else "stop"
            choice = {"index": 0, "message": message, "finish_reason": finish}
            self._json(200, {**base, "object": "chat.completion", "choices": [choice], "usage": usage})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send(delta: dict[str, Any], finish: str | None = None) -> None:
            choice = {"index": 0, "delta": delta, "finish_reason": finish}
            chunk = {**base, "object": "chat.completion.chunk", "choices": [choice]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        send({"role": "assistant", "content": ""})
        send({"reasoning": "thinking"})
        if body.get("mock_cut"):
            send({"content": "partial "})
            return  # connection closes: no finish_reason, no usage, no [DONE]
        if tool_calls:
            send({"tool_calls": [{"index": 0, **tool_calls[0]}]})
            send({}, "length" if cut else "tool_calls")
        else:
            for word in TEXT.split(" "):
                send({"content": word + " "})
            send({}, "length" if cut else "stop")
        if (body.get("stream_options") or {}).get("include_usage"):
            chunk = {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> None:
    global MODEL, MAX_MODEL_LEN, API_KEY, COMPLETION_TOKENS, TEXT, PROBE, FAIL_ONCE_AT, LENGTH_AT, PROBE2, DELAY_AT, DELAY
    global PREEMPTIONS, PREEMPT_EVERY, OVERFLOW_AT
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8399)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-model-len", type=int, default=MAX_MODEL_LEN)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--completion-tokens", type=int, default=COMPLETION_TOKENS)
    parser.add_argument("--text", default=TEXT)
    parser.add_argument("--probe", default="", help="shell command to request once through a bash-like tool")
    parser.add_argument("--probe2", default="", help="a second shell command, after the first tool result")
    parser.add_argument("--delay-at", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--fail-once-at", type=int, default=0)
    parser.add_argument("--length-at", type=int, default=0)
    parser.add_argument("--overflow-at", type=int, default=0, help="answer this chat request with a context-overflow 400")
    parser.add_argument("--preemptions", type=int, default=0, help="initial value of the /metrics counter")
    parser.add_argument("--preempt-every", type=int, default=0, help="count one preemption per N chat requests")
    parser.add_argument("--no-metrics", action="store_true", help="answer 404 on /metrics")
    args = parser.parse_args()
    FAIL_ONCE_AT, LENGTH_AT = args.fail_once_at, args.length_at
    OVERFLOW_AT = args.overflow_at
    PREEMPTIONS, PREEMPT_EVERY = (-1 if args.no_metrics else args.preemptions), args.preempt_every
    PROBE2, DELAY_AT, DELAY = args.probe2, args.delay_at, args.delay
    MODEL, MAX_MODEL_LEN, API_KEY = args.model, args.max_model_len, args.api_key
    COMPLETION_TOKENS, TEXT, PROBE = args.completion_tokens, args.text, args.probe
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
