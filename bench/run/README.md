# bench/run — running agents against dev-agent-bench

## What this is

The runner starts an agent (opencode or OpenHands) on the Harbor task. The model
is any OpenAI-compatible vLLM endpoint (`--upstream`). Between the harness and
the model sits `proxy.py`: it swaps a throwaway per-trial key for the real API
key, logs traffic, enforces the token budget, and marks step boundaries. The
trial record is `run.json`. Everything here runs on the host, outside the task
container; the agent sees only a base URL and the throwaway key.

```
harness (container) --> proxy.py (host) --> vLLM endpoint (--upstream)
                          ^ control API
Harbor (host) --plugin--> bench_harbor_plugin.py   (step boundaries)
run_trial.py              checks, starts proxy and Harbor, writes run.json
```

## Quick start

```bash
# key: env BENCH_VLLM_API_KEY, or file ~/.config/dev-agent-bench/vllm_api_key (chmod 600)
python3 bench/run/run_trial.py --model qwen3.8-27b --harness opencode --steps 1
python3 bench/run/run_trial.py --model qwen3.8-27b --harness openhands --steps 5

python3 bench/run/run_batch.py --model gemma-4-31b --harnesses opencode --repeats 3 --steps 5 --matrix
```

`--model` is a key of `serving/models.lock.json`; `--profile` defaults to `P1`
(131072 + fp8 KV; the model guard requires the served `max_model_len` to match).
Output goes to `--out` (default `~/bench-runs`, or `$BENCH_RUNS_DIR`):

```
<out>/<job>/
  run.json                     only if the trial did not start
  bench-run/harbor-job.json    Harbor job config (keys redacted)
  bench-run/traffic/           proxy log: state.json, index.jsonl, <step>/NNNNNN.json
  bench-run/task/bench/        the task copy Harbor ran (steps trimmed by --steps)
  <trial>/run.json             the trial record
  <trial>/steps/<step>/agent/trajectory.json   ATIF
```

Exit codes: `0` ok, `2` `infra_failure` (rerun; not in statistics), `3` trial did
not start (upstream, proxy, model guard, or the upstream probe).

Limits per step (`harnesses.json` -> `limits`): TTL 2 h of agent time
(`agent_timeout_sec` 7200; a step over it ends as `timeout` - a failed step,
not `infra_failure`), 1M completion tokens, 300 turns.

## Harnesses used in the published results

| Harness | Version | CLI flag | In published matrix |
|---|---|---|---|
| opencode (`opencode-ai`, npm) | 1.18.34 | `--harness opencode` | yes |
| OpenHands (`openhands-ai`, PyPI) | 1.6.0 | `--harness openhands` | yes |
| OpenHands SDK (`openhands-sdk`, PyPI) | 1.50.1 | `--harness openhands-sdk` | no — supported by the runner, not used in the published results |

Pinned elsewhere: Harbor 0.23.0 (`uvx --from harbor==0.23.0`); Node in the image
22.23.3 (sha256-checked) so Harbor finds `node`/`npm` without a per-trial apt
install. Versions live in `harnesses.json`. Default proxy port is `8301`; the
default `--upstream` is `http://127.0.0.1:8300` (override with `--upstream`).

A batch without `--harnesses` walks `opencode` → `openhands-sdk` → `openhands`
(step 1, one repeat) — that is the runner default queue, not the published
matrix. For a matrix cell use `--harnesses` explicitly, as in Quick start.

## Reference

### A batch: one model, a queue of trials

```bash
python3 bench/run/run_batch.py --model qwen3.8-27b
python3 bench/run/run_batch.py --model gemma-4-31b --harnesses opencode --repeats 3 --steps 5 --matrix
```

One `run_trial.py` process per trial, one after another, nobody in between:
for each of `--harnesses`, `--repeats` trials in a row (default 1), the first
`--steps` steps (default 1; 5 = the whole task). The model on the server is
never switched by the runner: one batch is one model. Everything goes under
`<out>/batch-<time>-<model>/`: the usual job directories, `<harness>-r<N>.log`,
`batch.json` and `batch-summary.md` - an overview with a line per trial
(status, rewards and end reasons by step, time, tokens, library versions,
preemptions, flags) and a table per trial with a line per step (end reason,
reward and failed criteria, turns, tokens, peak context, answers at the output
limit, longest response, compactions, errors, tool calls, step time, turn
categories). The options of `run_trial.py` (`--profile`, `--budget`,
`--agent-timeout`, `--upstream`, ...) are accepted.

Flags, collected over all steps and always shown in the summary: the trial did
not start; a step ended `crashed` / `infra_failure`; vLLM answered an HTTP
error other than a context overflow (a 400 "maximum context length" is only
counted: `turn_categories.http_errors`); no tool call was parsed in a step;
the code snapshot of a step is `fallback` or `missing`.
When the batch stops: by default after any trial with a flag (review mode);
with `--matrix` only after a trial that did not start or is `infra_failure`;
with `--keep-going` never. In `--matrix` mode a trial that did not start (no
model request: upstream, guard, image build, network) is tried again after
`--retry-wait` seconds (default 120), up to 3 attempts, before the batch
stops; a trial that started and ended `infra_failure` is run again up to 2
times, after the server answers on `/v1/models` again (polled every 30 s for up
to `--server-wait` seconds, default 1800; no answer in that time stops the
batch). Failed attempts stay on disk and in `batch.json` (`attempts`). Exit code `0` - no flags, `1` - stopped or flagged,
`3` - the first trial did not start.

**Preemptions.** `run_trial.py` reads `vllm:num_preemptions_total` from
`<upstream>/metrics` (directly, no key) before and after the trial:
`serving.preemptions` in `run.json`, with `during_trial` = the difference. The
counter belongs to the whole server. If `/metrics` cannot be read, the snapshot
carries a curl command for the server and the batch summary prints it.

**Upstream probe.** Before the agent of the first step starts, the Harbor plugin
asks vLLM directly from inside the task container
(`http://host.docker.internal:<upstream port>/v1/models`), without a key and with
the trial key. Expected: 401/403 or unreachable. Anything else, or a probe that
cannot run, locks the proxy and fails the step before the agent starts; the
result is `upstream_probe` in `run.json`.

### Proxy

* **Key swap.** The container holds a random per-trial key; the proxy replaces it
  with the real vLLM key. The agent cannot reach vLLM around the proxy.
* **Traffic log.** One JSON file per exchange: request body as sent by the
  harness, response body or raw SSE stream plus the assembled message, `usage`.
  `index.jsonl` has one line per exchange with the sampling fields sent,
  `chat_template_kwargs`, and which reasoning field came back in history.
* **Step boundaries.** The Harbor plugin calls `POST /_control/step` on
  `AGENT_START` and `/_control/step-end` on `AGENT_END`; counters are per step.
  Generation requests outside a step are refused (`no_active_step`).
  Such a refusal makes the trial `infra_failure`, except a request that came
  after the end of a step that ended with `AgentTimeoutError` and before the
  next step: Harbor stopped the agent at the TTL and it was still sending. That
  one is a warning (`outside_step_refusals.after_agent_timeout`).
* **Budget.** Completion tokens per step (reasoning included). Checked before
  each request; once `used >= budget` the proxy answers HTTP 403
  `budget_exhausted` (not retried by any of the harnesses). Requests already in
  flight are not cut, so the overshoot is at most `max_inflight` responses of
  up to the output limit each (32000 tokens). `max_inflight` is in `run.json`
  per step.
* **Paths.** Only `GET /v1/models` and `POST /v1/chat/completions` are served —
  the paths the harnesses and the runner use. Anything else gets 404 from the
  proxy and never reaches vLLM (`rejected_paths` in `run.json`).
* **Stripped fields.** `reasoning_effort` is removed before forwarding (thinking
  is configured on the server; vLLM rejects `high` for Qwen3.8) and recorded in
  `proxy_stripped`; the logged request body stays as the harness sent it.
* **Usage of streams.** The proxy adds `stream_options.include_usage` when the
  harness did not, and hides the extra usage chunk from the harness. A stream
  cut before its usage chunk is counted by delta chunks; such responses are
  counted in `usage_estimated` per step.
* **Output limit.** A generation request with neither `max_tokens` nor
  `max_completion_tokens` gets `max_tokens: 32000` (`limits.max_output_tokens`),
  recorded in `proxy_injected`. A limit sent by the harness is never changed.
* **Auxiliary requests.** A generation request without `tools` (opencode's
  session title, summaries) is marked `auxiliary`: its tokens spend the step
  budget, but it is not a turn.
* **Waiting for the upstream.** When vLLM cannot be reached before a response
  has started (connection refused or reset), the proxy polls `/v1/models` every
  10 s for up to 15 min (`--upstream-wait`, `--upstream-poll`) and then sends
  the same request again; the harness just sees a slow answer. A stream that
  breaks in the middle is never sent again. Per step: `upstream_waits` and
  `upstream_wait_seconds` in `run.json`; per request: `upstream_waits` in the
  traffic log. If the upstream does not come back in time, the request fails
  with 502 as before. A step that ends `timeout` after more than 10 min of such
  waiting is `infra_failure`.
* **Upstream errors.** Counted per step, together with retries (a request that
  repeats the body of the one that just failed). A step is `infra_failure` only
  when its last model request failed upstream and nothing succeeded after it.
* **Sampling check.** Each sampling field sent is compared with the vendor
  values from the lock; mismatches are counted, never rewritten.

Self-test (no GPU, no Docker): `python3 bench/run/tests/test_proxy.py` (proxy),
`python3 bench/run/tests/test_runjson.py` (turn categories, library versions,
batch stop conditions, preemption snapshot).

### Harness configuration

**Instruction through a file.** Harbor puts the whole instruction on the harness
command line; the instruction contains the service start command, so an agent
that runs `pkill -f "uvicorn tracksvc.api.app:app"` kills its own harness.
`bench_agents.py` holds thin subclasses of Harbor's three agents that upload the
instruction to `/installed-agent/bench-task.md` and rewrite only the launch
command: opencode reads it from stdin, OpenHands gets `--file`, the SDK runner is
started by a small launcher. No process has the instruction in its argv. If the
harness is still ended by a signal, `run.json` shows it in `harness_signal`.

**Client timeout.** One HTTP timeout for all three, `limits.client_timeout_sec`
(1800 s): opencode — provider options `timeout`, `headerTimeout`, `chunkTimeout`;
OpenHands — `LLM_TIMEOUT`; SDK — `LLM.timeout`, set by the launcher. The value
the Python clients announce (`x-stainless-read-timeout`) is recorded per step in
`client_timeouts_seen_sec`; opencode announces nothing. Responses a client
abandoned anyway are counted in `client_aborted` with their tokens.

Built by `run_trial.py` from `harnesses.json` and the lock.

* **opencode** — custom provider `@ai-sdk/openai-compatible`; `small_model` is
  the same model; `plugin: []`; `autoupdate: false`; `share: disabled`;
  `agent.build.steps = 300`; context limit = profile `max_model_len`, output
  limit 32000. No sampling fields configured. `model_options` in
  `harnesses.json` is merged into the model entry (empty by default).
* **OpenHands SDK** (`--harness openhands-sdk`, Harbor agent `openhands-sdk`) —
  `openai/<served name>` through LiteLLM; `MAX_ITERATIONS=300`. It sends no
  sampling fields and no output limit (the proxy adds `max_tokens: 32000`); it sends `reasoning_effort: "high"` and
  `prompt_cache_key`. The `temperature` kwarg is passed but the SDK does not put
  it on the wire. The ATIF trajectory is written only when the run ends
  normally: after `budget_exhausted` or a crash there is none (the traffic log
  is complete). **History condenser:** Harbor's runner builds the agent without
  one, so a full context window ended the run (`ContextWindowExceededError`).
  Our launcher adds the condenser of the OpenHands CLI - the SDK's
  `default_condenser` on the same model through the proxy
  (`LLMSummarizingCondenser`, SDK 1.50.1: `max_size=80`, `keep_first=4`, no
  token limit). What was configured is in `steps[].condenser`, the number of
  summaries in `steps[].turn_categories.condensations`; a summarizing request
  has no tools, so it is `auxiliary`: it spends the budget and is not a turn.
* **OpenHands** (`--harness openhands`) — `openai/<served name>` through
  LiteLLM; `MAX_ITERATIONS=300`; it always sends `temperature` and `top_p`, so
  the runner sets them to the vendor values from the lock (`top_p` 1.0 when the
  vendor sets none); output limit 32000. Harbor 0.23.0 launches
  `openhands.core.main` (last release that still provides it: 1.6.0).

#### Confounder: reasoning in history

vLLM v0.30.0 returns reasoning in the `reasoning` field and renders past
reasoning into the next prompt only from that field. Harness defaults are kept:

| Harness | Past reasoning sent back as | Reaches the model |
|---|---|---|
| opencode 1.18.34 | `reasoning_content` | no |
| OpenHands SDK 1.50.1 | not sent | no |
| OpenHands 1.6.0 | not sent | no |

So no harness preserves thinking across turns, equally for all three. opencode
could (`"interleaved": {"field": "reasoning"}` via `model_options`); this is
deliberately not enabled. `index.jsonl` records the field per request
(`history_reasoning`).

### run.json

Trial: `status` (`ok` | `infra_failure`), model (repo, revision, served name,
what `/v1/models` reported), serving profile and vLLM image digest, harness
name and version (pinned and reported), Harbor version, image id, hashes of
contract / AGENTS.md / step instructions, timestamps.

Also: `environment` — image id and `agent_views`: what `uv` (path, version) and
`python` resolve to for the agent's processes and shells inside the trial;
`warnings` — e.g. the agent resolves a `uv` of another version than the image's.
Harbor's OpenHands installers download uv with the unversioned installer into
the agent's `~/.local/bin`; our agent classes pin that download to
`image_uv_version` (`harnesses.json`), so the agent's shells see the same
version as the image (another path, same version: not a warning).

Per step: `end_reason`, `last_message_is_question`, tokens (prompt, completion,
reasoning), `turns` (without auxiliary requests), `auxiliary_requests`,
`length_finishes` (responses with `finish_reason: length`), `last_response_length`
(the step is `completed`, but its last answer was cut by the output limit),
`max_response_completion_tokens`, `history_shrinks` (the harness sent a shorter
history than before: compaction), `max_inflight`, `rejected_paths`,
`usage_estimated`, `client_aborted`, `client_aborted_completion_tokens`,
`client_timeouts_seen_sec`, `harness_signal`, `proxy_stripped`, `upstream_errors`,
`upstream_retries`, wall time, peak context, sampling fields sent, reward, paths
to the ATIF trajectory and the traffic log.

`turn_categories` per step (`turncats.py`, from the traffic log; the same code
for all three harnesses, SSE streams of opencode included): turns, tool calls,
completion tokens with their share, generation seconds (`model_sec`) and
seconds until the next request (`tools_sec`) for `library_read` (sources under
`site-packages`), `experiment` (`python -c`, heredocs), `file_edit` (editor
tools and shell redirects: `cat > f`, `cat <<EOF > f`, `tee f`, `printf … > f`
- checked before everything else), `rm_recreate` (`rm`, and a second whole-file
write of the same path), `checks` (ruff, mypy, pytest, alembic run as a
command, not named as an argument), `service_run`, `other`; a Python cell of
OpenHands (`execute_ipython_cell`) is classified by its code - a file write,
shell lines (`!cmd`, `%%bash`), library sources, otherwise `experiment`; plus
`library_read_roots` (which environment the sources were read from),
`tool_calls_unparsed_arguments`, `longest_response`, `output_limit_hits`
(answers whose completion tokens reached the request's output limit, counted
by tokens whatever `finish_reason` says), `invalid_tool_names` (tool calls
whose name is not among the tools offered in the request), `tool_results` (returned /
failed) and `last_successful_tool_call` (`sec_from_step_start`: how far into
the step the last tool result without a failure mark came back; compare with
`wall_time_sec`). A turn with several tool
calls is split equally between them. The tokens of a turn are mostly reasoning
and are attributed to the action the turn ended with. For a finished run:
`python3 bench/run/turncats.py <job>/bench-run/traffic step-1`.

`environment.library_versions`: versions of sqlalchemy, asyncpg, starlette,
fastapi, pytest-asyncio in the harness's Python (`/opt/openhands-venv`,
`/opt/openhands-sdk-venv`) and in `/app/.venv`, before the first step and after
every step; `diverged` is true when a library is installed in both with
different versions (null for opencode, which has no Python). Recorded only:
the agent's PATH is not changed. A divergence is also listed in `warnings`.

`snapshot` per step: what is left of the step's code snapshot (the
`bench-snapshot` hook) - `status` `ok` (a git bundle), `fallback` (git could
not make one because the agent broke `.git`; an archive of the working copy
without `.venv` and `.git` was kept, `snapshot-<step>.tar.gz`) or `missing`
(neither), with `error`, `bundle`, `archive`. `fallback` and `missing` are
flags in the batch summary; they never stop a batch.

`no_final_answer`, `turn_limit_hit`, `iterations` per step. `no_final_answer`:
the last turn is not the agent's own final answer (a `finish` call of OpenHands
or a message without a tool call) - the harness stopped the agent; the end
reason of such a step is still `completed` when the harness process exited
normally. `turn_limit_hit`: the harness spent `limits.max_turns` iterations;
OpenHands counts every model call (also the ones answered with a
context-overflow 400) and every condensation, so `iterations` = turns +
overflow answers + history shrinks. `template_rejected_harness_message`:
opencode ends a session at its step limit by asking for a final text after its
own `assistant` message; the Mistral chat template refuses such a request
(HTTP 400 "the last message is from the assistant"), opencode exits with an
error and the step is `crashed` - the rejected request is counted as the last
iteration, so the step is also `turn_limit_hit`. `--max-turns N` of
`run_trial.py` / `run_batch.py` gives the harness another limit for one run;
`limits.max_turns` in `run.json` is the limit actually used. A `completed` step without a final answer
that did not hit the limit was stopped by something else. `loop_detected`
(`detected`, `message`): the harness's own loop detector ended the step - read
from the harness log of the step (OpenHands: `stuck.py: ... loop detected`,
`AgentStuckInLoopError`); the process exits normally, so the end reason stays
`completed`. All three are flags in the batch summary and never stop a batch.

`nonzero_exit_after_context_overflow`: the harness process returned a non-zero
code although the step was finished. opencode records a context-overflow 400
as a session error, compacts the history, goes on to the end and then exits
with code 1. Such a step is `completed` with this flag when the process ended
by itself (no signal), the step's only HTTP errors are context overflows, and
after the last of them the model answered again and its last answer is a
normal final message (`finish_reason: stop`, no tool call).

Saved trials can be rebuilt with the current rules:
`python3 bench/run/recompute.py <job dir>...` writes `run.recomputed.json`
next to each `run.json` (the original is not changed) and prints the steps
whose end reason changed and the trials whose status changed.

**Task variants.** `--task-dir <dir>` (`run_trial.py`, `run_batch.py`) runs a
built variant of the task instead of `bench/` - the G2 ablation is
`bench-g2/build/bench` (see `bench-g2/README.md`). `hashes` then describe that
directory, `task_variant` is its `variant.json`, and every step of a G2 trial
has a `g2` block (`g2.py`): hook runs and rejections by failed target, the
agent's commits and those without a green hook run, `final_commit`, the state
of the hook and of the files it depends on, bypass patterns in the agent's
commands (`--no-verify`, `core.hooksPath`, edits of the protected files) and
the resulting `bypass` flags. A trial of `bench/` has neither key.
`python3 bench/run/g2.py <job dir>...` prints the table per step.

`end_reason` priority: `infra_failure` (the step ended on an upstream error,
Harbor environment/setup/verifier failure, no verifier result) >
`budget_exhausted` (the proxy refused at least one request) > `timeout`
(`AgentTimeoutError` in Harbor's `step_results[].exception_info`; the
trial-level `exception_info` stays empty for it) > `crashed` (other agent
exception, or no model response at all) > `completed`.
