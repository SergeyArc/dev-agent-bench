"""Smoke tests of the vLLM server for dev-agent-bench.

Run (inside the vllm image, host network):
  python3 smoke.py --marker '<think>' [--no-thinking] [--skip-long] [--out file.json]
The API key is read from /run/api_key. Sampling is not sent: server defaults are checked.
"""
import argparse, json, re, sys, time
import requests

BASE = "http://127.0.0.1:8300"
KEY = open("/run/api_key").read().strip()
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
LEAK = re.compile(r"<tool_call>|</tool_call>|<function=|\[TOOL_CALLS\]|<\|tool_call|"
                  r"\"name\"\s*:\s*\"get_(weather|time)\"")
TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather", "description": "Current weather for a city",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string"}}, "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "get_time", "description": "Current local time in an IANA timezone",
        "parameters": {"type": "object", "properties": {
            "timezone": {"type": "string"}}, "required": ["timezone"]}}},
]
ASK = [{"role": "user", "content":
        "What is the weather in Paris and the current time in Asia/Tokyo? "
        "Call the tools, do not guess."}]
R = {}


def post(path, body, stream=False, timeout=900):
    r = requests.post(BASE + path, headers=H, json=body, stream=stream, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"{path} -> {r.status_code}: {r.text[:500]}")
    return r


def reasoning_of(msg):
    for k in ("reasoning_content", "reasoning"):
        if msg.get(k):
            return k, msg[k]
    return None, ""


def render(model, messages, kwargs=None):
    body = {"model": model, "messages": messages, "add_generation_prompt": True}
    if kwargs is not None:
        body["chat_template_kwargs"] = kwargs
    ids = post("/tokenize", body).json()["tokens"]
    txt = post("/detokenize", {"model": model, "tokens": ids}).json()["prompt"]
    return txt, len(ids)


def check_calls(calls):
    names, ok = [], True
    for c in calls:
        f = c.get("function") or {}
        names.append(f.get("name"))
        try:
            args = json.loads(f.get("arguments") or "")
            ok &= isinstance(args, dict) and bool(args)
        except Exception:
            ok = False
    ok &= set(names) <= {"get_weather", "get_time"} and len(names) >= 1
    return ok, names


def stream_chat(body):
    body = dict(body, stream=True, stream_options={"include_usage": True})
    r = post("/v1/chat/completions", body, stream=True)
    content, reasoning, calls, usage, times = "", "", {}, None, []
    for line in r.iter_lines():
        if not line or not line.startswith(b"data: "):
            continue
        data = line[6:]
        if data == b"[DONE]":
            break
        ev = json.loads(data)
        if ev.get("usage"):
            usage = ev["usage"]
        for ch in ev.get("choices", []):
            d = ch.get("delta", {})
            if d.get("content") or d.get("reasoning") or d.get("reasoning_content") \
                    or d.get("tool_calls"):
                times.append(time.time())
            content += d.get("content") or ""
            reasoning += d.get("reasoning") or d.get("reasoning_content") or ""
            for tc in d.get("tool_calls") or []:
                slot = calls.setdefault(tc["index"], {"function": {"name": "", "arguments": ""}})
                fn = tc.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""
                if tc.get("id"):
                    slot["id"] = tc["id"]
    return content, reasoning, [calls[i] for i in sorted(calls)], usage, times


ONLY = set()


def step(name, fn):
    if ONLY and name.split("_")[0] not in ONLY:
        return
    t = time.time()
    try:
        res = fn()
        res["dt_s"] = round(time.time() - t, 1)
    except Exception as e:  # do not abort the run; record as a failure
        res = {"pass": False, "error": repr(e)[:400]}
    R[name] = res
    print(f"[{'PASS' if res.get('pass') else 'FAIL'}] {name}: "
          f"{json.dumps({k: v for k, v in res.items() if k != 'pass'}, ensure_ascii=False)[:600]}",
          flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--marker", default="")
    ap.add_argument("--no-thinking", action="store_true")
    ap.add_argument("--skip-long", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--only", default="", help="comma-separated: a,t,b,c,d,e,f")
    a = ap.parse_args()
    ONLY.update(x for x in a.only.split(",") if x)

    m = requests.get(BASE + "/v1/models", headers=H, timeout=30).json()["data"][0]
    model, mml = m["id"], m.get("max_model_len")
    print(f"model={model} max_model_len={mml}", flush=True)
    R["model"], R["max_model_len"] = model, mml

    def a_plain():
        j = post("/v1/chat/completions", {"model": model, "max_tokens": 2048, "messages": [
            {"role": "user", "content": "Reply with exactly one word: pong"}]}).json()
        msg = j["choices"][0]["message"]
        k, rs = reasoning_of(msg)
        return {"pass": "pong" in (msg.get("content") or "").lower(),
                "content": (msg.get("content") or "")[:80], "reasoning_field": k,
                "reasoning_len": len(rs), "finish": j["choices"][0]["finish_reason"]}
    step("a_models_plain_chat", a_plain)

    def s_system():
        # What the template/tokenizer injects when the harness sent no system message.
        txt, n = render(model, [{"role": "user", "content": "ping"}])
        return {"pass": True, "prompt_tokens": n, "injected_default_system": n > 60,
                "mentions_vibe": "Vibe" in txt, "head": txt[:240]}
    step("s_default_system_prompt", s_system)

    if not a.no_thinking:
        def t_render():
            on, n_on = render(model, ASK)
            off, _ = render(model, ASK, {"enable_thinking": False})
            nosys_on = on
            withsys, _ = render(model, [{"role": "system", "content": "You are helpful."}] + ASK)
            return {"pass": a.marker in on and on != off,
                    "marker_default": a.marker in on, "marker_when_false": a.marker in off,
                    "marker_with_system": a.marker in withsys,
                    "differs_from_false": on != off, "tail_default": on[-120:],
                    "tail_false": off[-120:], "prompt_tokens": n_on}
        step("t_thinking_render", t_render)

    def b_tools_nonstream():
        j = post("/v1/chat/completions", {"model": model, "messages": ASK, "tools": TOOLS,
                                          "max_tokens": 8192}).json()
        msg = j["choices"][0]["message"]
        ok, names = check_calls(msg.get("tool_calls") or [])
        content = msg.get("content") or ""
        k, rs = reasoning_of(msg)
        R["_b_msg"] = msg
        return {"pass": ok and not LEAK.search(content), "names": names,
                "content_leak": bool(LEAK.search(content)), "content": content[:160],
                "reasoning_field": k, "reasoning_len": len(rs),
                "finish": j["choices"][0]["finish_reason"]}
    if ONLY and "c" in ONLY and "b" not in ONLY:
        b_tools_nonstream()
    step("b_tools_nonstream", b_tools_nonstream)

    def b_tools_stream():
        content, rs, calls, usage, _ = stream_chat(
            {"model": model, "messages": ASK, "tools": TOOLS, "max_tokens": 8192})
        ok, names = check_calls(calls)
        return {"pass": ok and not LEAK.search(content), "names": names,
                "content_leak": bool(LEAK.search(content)), "content": content[:160],
                "reasoning_len": len(rs), "usage": usage}
    step("b_tools_stream", b_tools_stream)

    def c_multiturn():
        # Call -> result -> ... chain until the final answer: the model may call
        # tools one at a time (sequentially), not both at once. Up to 4 rounds.
        msg = R.get("_b_msg") or {}
        if not (msg.get("tool_calls") or []):
            return {"pass": False, "error": "no tool_calls from test b"}

        def tool_out(n):
            return "18°C, light rain" if n == "get_weather" else "2026-10-01T15:04:00+09:00"

        def chain(mode):
            hist, names, rounds, content, r_last = list(ASK), [], 0, "", ""
            cur_calls, cur_content, cur_reason = msg["tool_calls"], msg.get("content") or "", \
                reasoning_of(msg)[1]
            while cur_calls and rounds < 4:
                rounds += 1
                am = {"role": "assistant", "content": cur_content, "tool_calls": cur_calls}
                if cur_reason:
                    am["reasoning"] = cur_reason
                hist.append(am)
                for c in cur_calls:
                    names.append(c["function"]["name"])
                    hist.append({"role": "tool", "tool_call_id": c.get("id", ""),
                                 "content": tool_out(c["function"]["name"])})
                body = {"model": model, "messages": hist, "tools": TOOLS, "max_tokens": 8192}
                if mode == "nonstream":
                    mm = post("/v1/chat/completions", body).json()["choices"][0]["message"]
                    cur_calls = mm.get("tool_calls") or []
                    cur_content, cur_reason = mm.get("content") or "", reasoning_of(mm)[1]
                else:
                    cur_content, cur_reason, sc, _, _ = stream_chat(body)
                    cur_calls = [{"id": c.get("id", ""), "type": "function",
                                  "function": c["function"]} for c in sc]
                r_last = cur_reason or r_last
            content = cur_content
            return {"rounds": rounds, "names": names,
                    "uses_result": ("18" in content) or ("rain" in content.lower()),
                    "final_has_calls": bool(cur_calls), "leak": bool(LEAK.search(content)),
                    "reasoning_len": len(r_last), "content": content[:200]}

        res = {m: chain(m) for m in ("nonstream", "stream")}
        calls = msg["tool_calls"]
        # Which reasoning field the template reads from history (turn after the last user).
        probe = {}
        for field in ("reasoning_content", "reasoning"):
            h2 = list(ASK) + [{"role": "assistant", "content": "", field: "PROBE-R-7731",
                               "tool_calls": calls[:1]},
                              {"role": "tool", "tool_call_id": calls[0].get("id", ""),
                               "content": "18°C"}]
            try:
                txt, _ = render(model, h2)
                probe[field] = "PROBE-R-7731" in txt
            except Exception as e:
                probe[field] = f"error: {e!r}"[:120]
        res["history_reasoning_rendered"] = probe
        res["pass"] = all(res[x]["uses_result"] and not res[x]["leak"]
                          and not res[x]["final_has_calls"] for x in ("nonstream", "stream"))
        return res
    step("c_multiturn_tool_result", c_multiturn)

    if not a.skip_long:
        def d_long():
            # Goal: prompt ~ max_model_len - 1024; completion fills the rest of the window to max_model_len.
            target = mml - 1024
            chunk = ("Line {i}: the archive ledger records shipment {i} of copper wire, "
                     "checked and sealed by the night clerk.\n")
            tail = "\nQuestion: which shipment number is on the last line? Answer briefly."

            def build(k):
                return "".join(chunk.format(i=i) for i in range(k)) + tail
            _, n200 = render(model, [{"role": "user", "content": build(200)}])
            lines = int(target / (n200 / 200))
            for _ in range(6):  # adjust to the actual render
                _, n = render(model, [{"role": "user", "content": build(lines)}])
                if n <= target:
                    break
                lines -= int((n - target) / (n200 / 200)) + 5
            out = mml - n
            j = post("/v1/chat/completions", {"model": model, "max_tokens": out,
                                              "messages": [{"role": "user",
                                                            "content": build(lines)}]},
                     timeout=1800).json()
            msg = j["choices"][0]["message"]
            u = j["usage"]
            return {"pass": u["prompt_tokens"] >= mml * 0.95,
                    "prompt_tokens": u["prompt_tokens"], "completion_tokens": u["completion_tokens"],
                    "total": u["prompt_tokens"] + u["completion_tokens"], "max_model_len": mml,
                    "expected_last": lines - 1, "content": (msg.get("content") or "")[:120],
                    "finish": j["choices"][0]["finish_reason"]}
        step("d_long_context", d_long)

    def e_decode():
        content, rs, _, usage, times = stream_chat({
            "model": model, "max_tokens": 2048, "ignore_eos": True,
            "messages": [{"role": "user", "content": "Write a long story about a lighthouse."}]})
        n = (usage or {}).get("completion_tokens", 0)
        span = times[-1] - times[0] if len(times) > 1 else 0
        return {"pass": n >= 2000 and span > 0, "completion_tokens": n,
                "ttft_to_last_s": round(span, 2),
                "decode_tok_s": round((n - 1) / span, 1) if span else None}
    step("e_decode_single_stream", e_decode)

    def f_metrics():
        txt = requests.get(BASE + "/metrics", headers=H, timeout=30).text
        vals = {}
        for name in ("vllm:num_preemptions_total", "vllm:num_requests_running",
                     "vllm:prompt_tokens_total", "vllm:generation_tokens_total"):
            s = sum(float(l.split()[-1]) for l in txt.splitlines()
                    if l.startswith(name) and not l.startswith("#"))
            vals[name] = s
        return {"pass": vals["vllm:num_preemptions_total"] == 0, **vals}
    step("f_preemptions", f_metrics)

    R.pop("_b_msg", None)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(R, f, ensure_ascii=False, indent=1)
    print("SMOKE_DONE", flush=True)


if __name__ == "__main__":
    main()
