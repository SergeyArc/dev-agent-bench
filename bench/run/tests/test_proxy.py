#!/usr/bin/env python3
"""Self-test of proxy.py against mock_vllm.py. Run: python3 bench/run/tests/test_proxy.py"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
UPSTREAM_KEY = "real-key"
CLIENT_KEY = "trial-key"
CONTROL = "control-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(
    port: int, path: str, payload: Any = None, *, key: str | None = CLIENT_KEY, control: str | None = None
) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if control:
        headers["X-Bench-Control"] = control
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def wait_port(port: int) -> None:
    for _ in range(100):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"port {port} did not open")


def upstream_wait_tests(check: Any, env: dict[str, str]) -> None:
    """The proxy waits for an unreachable upstream (a dropped connection) and sends the request again."""
    mock_port, proxy_port = free_port(), free_port()
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}]
    chat = {"model": "mock-model", "messages": [{"role": "user", "content": "hi"}], "tools": tools}

    def start_mock() -> subprocess.Popen[bytes]:
        return subprocess.Popen([sys.executable, str(HERE / "mock_vllm.py"), "--port", str(mock_port), "--api-key", UPSTREAM_KEY])

    def step_state(name: str) -> dict[str, Any]:
        _, body = call(proxy_port, "/_control/state", key=None, control=CONTROL)
        return dict(json.loads(body)["steps"][name])

    with tempfile.TemporaryDirectory() as traffic:
        proxy = subprocess.Popen(
            [sys.executable, str(HERE.parent / "proxy.py"), "--port", str(proxy_port), "--upstream", f"http://127.0.0.1:{mock_port}",
             "--traffic-dir", traffic, "--upstream-wait", "6", "--upstream-poll", "0.3"],
            env=env, stdout=subprocess.DEVNULL,
        )
        mocks: list[subprocess.Popen[bytes]] = []
        try:
            wait_port(proxy_port)
            call(proxy_port, "/_control/step", {"step": "wait-1"}, key=None, control=CONTROL)
            # The upstream is down when the request arrives and comes up 2 s later.
            threading.Timer(2.0, lambda: mocks.append(start_mock())).start()
            started = time.monotonic()
            status, body = call(proxy_port, "/v1/chat/completions", chat)
            took = time.monotonic() - started
            state = step_state("wait-1")
            check("upstream down, back in 2 s: the request is sent again and answered", status == 200 and 1.5 <= took < 6, (status, took, body[:200]))
            check("the wait is recorded: one wait, its seconds, no upstream error",
                  state["upstream_waits"] == 1 and 1.5 <= state["upstream_wait_seconds"] < 6
                  and state["upstream_errors"] == 0 and not state["ended_on_upstream_error"] and state["turns"] == 1,
                  {k: state[k] for k in ("upstream_waits", "upstream_wait_seconds", "upstream_errors", "ended_on_upstream_error", "turns")})
            index = [json.loads(line) for line in (Path(traffic) / "index.jsonl").read_text().splitlines()]
            check("the wait is in the traffic log of that request",
                  len(index[-1].get("upstream_waits") or []) == 1 and index[-1]["status"] == 200, index[-1].get("upstream_waits"))
            _, received = call(mock_port, "/_mock/requests", key=UPSTREAM_KEY)
            check("the upstream got the request exactly once", len(json.loads(received)["requests"]) == 1, received[:200])

            # A stream that breaks in the middle is not sent again.
            status, body = call(proxy_port, "/v1/chat/completions", {**chat, "stream": True, "mock_cut": True})
            _, received = call(mock_port, "/_mock/requests", key=UPSTREAM_KEY)
            state = step_state("wait-1")
            check("a stream broken in the middle is not retried",
                  len(json.loads(received)["requests"]) == 2 and state["upstream_waits"] == 1 and state["aborted_streams"] == 1,
                  (len(json.loads(received)["requests"]), state["upstream_waits"], state["aborted_streams"]))

            # The upstream stays down longer than the limit: the request fails as before.
            mocks[0].terminate()
            mocks[0].wait()
            call(proxy_port, "/_control/step", {"step": "wait-2"}, key=None, control=CONTROL)
            started = time.monotonic()
            status, body = call(proxy_port, "/v1/chat/completions", chat)
            took = time.monotonic() - started
            state = step_state("wait-2")
            check("upstream down longer than the limit: 502 after the limit, the step ends on an upstream error",
                  status == 502 and "upstream_unreachable" in body and 5 <= took < 15
                  and state["upstream_errors"] == 1 and state["ended_on_upstream_error"],
                  (status, took, state["upstream_errors"], state["ended_on_upstream_error"]))
            index = [json.loads(line) for line in (Path(traffic) / "index.jsonl").read_text().splitlines()]
            check("a wait that ended without an answer is counted in the seconds of the step and marked in the traffic log",
                  state["upstream_waits"] == 1 and 5.5 <= state["upstream_wait_seconds"] < 15
                  and [w.get("gave_up") for w in index[-1].get("upstream_waits") or []] == [True],
                  (state["upstream_waits"], state["upstream_wait_seconds"], index[-1].get("upstream_waits")))
        finally:
            proxy.terminate()
            for mock in mocks:
                mock.terminate()


def main() -> int:
    mock_port, proxy_port = free_port(), free_port()
    traffic = Path(tempfile.mkdtemp(prefix="proxy-test-"))
    mock = subprocess.Popen(
        [sys.executable, str(HERE / "mock_vllm.py"), "--port", str(mock_port), "--api-key", UPSTREAM_KEY,
         "--fail-once-at", "5", "--length-at", "7"]
    )
    env = {
        **os.environ,
        "BENCH_PROXY_UPSTREAM_KEY": UPSTREAM_KEY,
        "BENCH_PROXY_CLIENT_KEY": CLIENT_KEY,
        "BENCH_PROXY_CONTROL_TOKEN": CONTROL,
        "BENCH_PROXY_EXPECTED_SAMPLING": json.dumps({"temperature": 1.0, "top_p": 0.95}),
    }
    proxy = subprocess.Popen(
        [
            sys.executable,
            str(HERE.parent / "proxy.py"),
            "--port",
            str(proxy_port),
            "--upstream",
            f"http://127.0.0.1:{mock_port}",
            "--traffic-dir",
            str(traffic),
            "--budget",
            "100",
            "--inject-max-tokens",
            "16384",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
    )
    failures: list[str] = []

    def check(name: str, ok: bool, detail: Any = "") -> None:
        print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if not ok else ""))
        if not ok:
            failures.append(name)

    chat = {"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]}
    try:
        wait_port(mock_port)
        wait_port(proxy_port)

        status, body = call(proxy_port, "/v1/models")
        check("models pass through with key swap", status == 200 and "mock-model" in body, (status, body))
        status, _ = call(proxy_port, "/v1/models", key="wrong")
        check("wrong client key rejected", status == 401, status)
        status, _ = call(proxy_port, "/v1/models", key=UPSTREAM_KEY)
        check("real upstream key is not accepted from clients", status == 401, status)
        status, _ = call(proxy_port, "/_control/state", key=None, control="nope")
        check("control needs the token", status == 403, status)
        status, body = call(proxy_port, "/v1/chat/completions", chat)
        check("generation outside a step refused", status == 403 and "no_active_step" in body, (status, body))

        status, _ = call(proxy_port, "/_control/step", {"step": "step-1"}, key=None, control=CONTROL)
        check("step begins", status == 200, status)

        status, body = call(proxy_port, "/v1/chat/completions", {**chat, "temperature": 0.0})
        check("non-stream completion", status == 200 and "All done." in body, (status, body))

        status, body = call(proxy_port, "/v1/chat/completions", {**chat, "stream": True})
        check("stream relayed", status == 200 and "[DONE]" in body and "done." in body, (status, body))
        check("injected usage chunk hidden from client", '"usage"' not in body, body)

        stream_usage = {**chat, "stream": True, "stream_options": {"include_usage": True}, "top_p": 0.95}
        status, body = call(proxy_port, "/v1/chat/completions", stream_usage)
        check("requested usage chunk kept", status == 200 and '"usage"' in body, (status, body))

        # 3 x 40 completion tokens >= budget 100
        status, body = call(proxy_port, "/v1/chat/completions", chat)
        check("budget exhausted -> 403", status == 403 and "budget_exhausted" in body, (status, body))

        _, body = call(proxy_port, "/_control/state", key=None, control=CONTROL)
        step = json.loads(body)["steps"]["step-1"]
        check("completion tokens counted", step["completion_tokens"] == 120, step["completion_tokens"])
        check("requests without tools are auxiliary, not turns", step["turns"] == 0 and step["auxiliary_turns"] == 3, step)
        check("auxiliary tokens still spend the budget", step["auxiliary_completion_tokens"] == 120, step)
        check("peak context", step["peak_context_tokens"] == 140, step["peak_context_tokens"])
        check("budget flag", step["budget_exhausted"] and step["refused_budget"] == 1, step)
        sent = step["sampling_fields_sent"]
        check("sampling fields recorded", sent == {"temperature": [0.0], "top_p": [0.95]}, sent)
        check("sampling violation flagged once", step["sampling_violations"] == 1, step["sampling_violations"])

        call(proxy_port, "/_control/step-end", {}, key=None, control=CONTROL)
        call(proxy_port, "/_control/step", {"step": "step-2", "budget": 1000}, key=None, control=CONTROL)
        tool = {"type": "function", "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}}}
        agent_chat = {**chat, "tools": [tool]}
        status, _ = call(proxy_port, "/v1/chat/completions", agent_chat)
        check("counter resets on the next step", status == 200, status)
        # mock request 5 fails once with HTTP 500, the retry (6) succeeds, 7 ends with "length"
        status, _ = call(proxy_port, "/v1/chat/completions", agent_chat)
        _, body = call(proxy_port, "/_control/state", key=None, control=CONTROL)
        step2 = json.loads(body)["steps"]["step-2"]
        check("upstream 500 relayed and pending", status == 500 and step2["ended_on_upstream_error"], (status, step2))
        status, _ = call(proxy_port, "/v1/chat/completions", agent_chat)
        status_len, _ = call(proxy_port, "/v1/chat/completions", agent_chat)
        _, body = call(proxy_port, "/_control/state", key=None, control=CONTROL)
        step2 = json.loads(body)["steps"]["step-2"]
        check(
            "retry recognised, error recovered",
            status == 200 and step2["upstream_errors"] == 1 and step2["upstream_retries"] == 1
            and not step2["ended_on_upstream_error"],
            step2,
        )
        check("finish_reason=length counted", status_len == 200 and step2["length_finishes"] == 1, step2)
        # Output limit: added only when the harness sent none.
        for extra in ({"max_tokens": 512}, {"max_completion_tokens": 256}):
            call(proxy_port, "/v1/chat/completions", {**agent_chat, **extra})
        request = urllib.request.Request(f"http://127.0.0.1:{mock_port}/_mock/requests")
        with urllib.request.urlopen(request, timeout=10) as response:
            received = json.loads(response.read())["requests"]
        check("no limit from harness -> max_tokens 16384 reaches upstream", received[0].get("max_tokens") == 16384, received[0])
        check("harness max_tokens untouched", received[-2] == {"model": "mock-model", "max_tokens": 512}, received[-2])
        check(
            "harness max_completion_tokens untouched, no max_tokens added",
            received[-1] == {"model": "mock-model", "max_completion_tokens": 256},
            received[-1],
        )
        records = [json.loads(p.read_text()) for p in sorted((traffic / "step-2").glob("*.json"))]
        check(
            "injection recorded in proxy_injected, request body kept as sent",
            records[0]["proxy_injected"] == {"max_tokens": 16384}
            and "max_tokens" not in records[0]["request_body"]
            and "proxy_injected" not in records[-1] and "proxy_injected" not in records[-2],
            [r.get("proxy_injected") for r in records],
        )
        _, body = call(proxy_port, "/_control/state", key=None, control=CONTROL)
        step2 = json.loads(body)["steps"]["step-2"]
        check("turns with tools counted", step2["turns"] == 5 and step2["auxiliary_turns"] == 0, step2)
        check("last response kept", step2["last_response"]["content_tail"] == "All done.", step2["last_response"])

        files = sorted(p.name for p in (traffic / "step-1").glob("*.json"))
        check("one traffic file per exchange", len(files) == 4, files)
        stream_record = json.loads((traffic / "step-1" / files[1]).read_text())
        check(
            "stream record has raw SSE, assembled message, reasoning field",
            "data: [DONE]" in stream_record["response_stream_raw"]
            and stream_record["response_assembled"]["reasoning_field"] == "reasoning"
            and stream_record["proxy_injected"] == {"stream_options.include_usage": True, "max_tokens": 16384},
            stream_record.keys(),
        )
        text = (traffic / "step-1" / files[0]).read_text()
        check("keys are not written to the log", UPSTREAM_KEY not in text and CLIENT_KEY not in text)
        lines = (traffic / "index.jsonl").read_text().splitlines()
        check("index.jsonl has a line per exchange", len(lines) == 12, len(lines))

        # --- proxy hardening ------------------------------------------------
        call(proxy_port, "/_control/step", {"step": "step-3", "budget": 100000}, key=None, control=CONTROL)
        status, _ = call(proxy_port, "/v1/chat/completions", {**agent_chat, "reasoning_effort": "high"})
        request = urllib.request.Request(f"http://127.0.0.1:{mock_port}/_mock/requests")
        with urllib.request.urlopen(request, timeout=10) as response:
            received = json.loads(response.read())["requests"]
        check("reasoning_effort removed before upstream", status == 200 and "reasoning_effort" not in received[-1], received[-1])
        records = sorted((traffic / "step-3").glob("*.json"))
        record = json.loads(records[-1].read_text())
        check(
            "removal recorded in proxy_stripped, logged body kept as sent",
            record["proxy_stripped"] == {"reasoning_effort": "high"}
            and record["request_body"]["reasoning_effort"] == "high",
            record.get("proxy_stripped"),
        )
        status_c, _ = call(proxy_port, "/v1/completions", {"model": "mock-model", "prompt": "x"})
        status_e, _ = call(proxy_port, "/v1/embeddings", {"model": "mock-model", "input": "x"})
        status_t, _ = call(proxy_port, "/tokenize", {"model": "mock-model", "prompt": "x"})
        check("paths outside the allowlist -> 404", (status_c, status_e, status_t) == (404, 404, 404), (status_c, status_e, status_t))
        before = len(json.loads(urllib.request.urlopen(request, timeout=10).read())["requests"])
        check("rejected paths never reach upstream", before == len(received), (before, len(received)))
        results: list[int] = []
        threads = [
            threading.Thread(
                target=lambda: results.append(call(proxy_port, "/v1/chat/completions", {**agent_chat, "mock_delay": 1.0})[0])
            )
            for _ in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        status, body = call(proxy_port, "/v1/chat/completions", {**agent_chat, "stream": True, "mock_cut": True})
        # a shorter history than the previous request: compaction
        call(proxy_port, "/v1/chat/completions", {**agent_chat, "messages": []})
        _, body = call(proxy_port, "/_control/state", key=None, control=CONTROL)
        step3 = json.loads(body)["steps"]["step-3"]
        check("max_inflight counts parallel requests", results == [200, 200, 200] and step3["max_inflight"] == 3, (results, step3["max_inflight"]))
        check("inflight back to zero", step3["inflight"] == 0, step3["inflight"])
        check(
            "rejected_paths counted with the paths",
            step3["rejected_paths"] == 3
            and step3["rejected_path_list"] == ["POST /v1/completions", "POST /v1/embeddings", "POST /tokenize"],
            step3,
        )
        check(
            "cut stream: usage_estimated counted",
            step3["usage_estimated"] == 1 and step3["aborted_streams"] == 1 and step3["estimated_completion_tokens"] == 2,
            step3,
        )
        check("history shrink counted", step3["history_shrinks"] == 1, step3["history_shrinks"])
        check("longest single response tracked", step3["max_response_completion_tokens"] == 40, step3["max_response_completion_tokens"])
        check("stripped field values recorded", step3["stripped_fields"] == {"reasoning_effort": ["high"]}, step3["stripped_fields"])
        # A client that hangs up before the answer: tokens are spent, the response is counted as aborted.
        def hang_up(payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            head = (
                f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {CLIENT_KEY}\r\n"
                f"Content-Type: application/json\r\nx-stainless-read-timeout: 1800.0\r\n"
                f"Content-Length: {len(data)}\r\n\r\n"
            ).encode()
            with socket.create_connection(("127.0.0.1", proxy_port)) as sock:
                sock.sendall(head + data)
                time.sleep(0.3)

        hang_up({**agent_chat, "mock_delay": 1.0})
        hang_up({**agent_chat, "mock_delay": 1.0, "stream": True})
        time.sleep(1.5)
        _, body = call(proxy_port, "/_control/state", key=None, control=CONTROL)
        step3 = json.loads(body)["steps"]["step-3"]
        check(
            "client_aborted counts abandoned responses and their tokens",
            step3["client_aborted"] == 2 and step3["client_aborted_completion_tokens"] >= 40,
            (step3["client_aborted"], step3["client_aborted_completion_tokens"]),
        )
        check("announced client timeout recorded", step3["client_timeouts_seen"] == ["1800.0"], step3["client_timeouts_seen"])
        status, _ = call(proxy_port, "/v1/chat/completions", agent_chat)
        _, body = call(proxy_port, "/_control/state", key=None, control=CONTROL)
        check("a normal response is not counted as aborted", status == 200 and json.loads(body)["steps"]["step-3"]["client_aborted"] == 2)
        call(proxy_port, "/_control/lockdown", {}, key=None, control=CONTROL)
        status, body = call(proxy_port, "/v1/models")
        check("lockdown refuses everything", status == 403 and "trial_locked" in body, (status, body))
        call(proxy_port, "/_control/step", {"step": "step-2b"}, key=None, control=CONTROL)

        mock.terminate()
        mock.wait()
        status, body = call(proxy_port, "/v1/chat/completions", chat)
        check("upstream down while locked: still refused before upstream", status == 403, status)
    finally:
        proxy.terminate()
        mock.terminate()
    upstream_wait_tests(check, env)
    print(f"\n{'OK' if not failures else 'FAILED'}: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
