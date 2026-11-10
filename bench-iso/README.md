# bench-iso — isolated variant: step N from the reference state of step N−1

Isolated single-step variant: one step N. The agent starts not from what an agent left
at the earlier steps, but from the reference solution of step N−1, committed as the
initial commit of `/app`. Model, harness, limits, verifier and hidden tests are those of
G0 (`bench/`). Optional `--task-md` / `--agents-md` flags replace the step wording or
project rules for spec-edit experiments without changing earlier-step code.

`bench/` is never edited. The variant is **built** from it, like `bench-g2`:

```bash
python3 bench-iso/build_variant.py --step 3                      # -> bench-iso/build/iso-s3/bench (git-ignored)
python3 bench-iso/build_variant.py --step 3 --task-md FILE       # ISO-SPEC: task.md of the step replaced
python3 bench-iso/build_variant.py --step 3 --agents-md FILE     # ISO-AGENTS: /app/AGENTS.md replaced
python3 bench-iso/build_variant.py --step 3 --task-md A --agents-md B --out DIR
python3 bench-iso/build_variant.py --hash                        # hash of bench/ without run/ and report/
python3 bench-iso/tests/test_iso.py                              # self-test, no Docker
```

The directory given to `--out` must be named `bench` (Harbor is started with `-p bench`
from its parent) and is replaced only if it is a built variant.

## What differs from G0

| Where | Difference |
|---|---|
| `environment/app/` (initial commit of `/app`) | + the reference tree after step N−1: the solutions of steps 1…N−1 applied one after another, as their `solve.sh` does (files of `src/`, `tests/`, `migrations/` missing in the next tree are removed). N = 1: nothing added |
| `environment/app/AGENTS.md` | `--agents-md`: replaced by the given file |
| `steps/` | only `step-N` is kept |
| `steps/step-N/task.md` | `--task-md`: replaced by the given file |
| `steps/step-N/instruction.md` | recomposed by the composer of `bench/`; without `--task-md` and `--agents-md` equal to G0 |
| `task.toml` | task name `local/dev-agent-bench-iso-s<N>`; only the `[[steps]]` block of `step-N` (`BENCH_STEP = "N"`) |
| `variant.json` | what was built and from what |

`tests/` (verifier, hidden tests), `environment/Dockerfile`, `pyproject.toml`, `uv.lock`,
`ruff.toml`, `mypy.ini`, the solution and the rubric of the step are byte-identical to G0.
The Dockerfile commits whatever is in `environment/app/`, so the reference tree becomes
the `Initial commit` without any change of the image recipe.

## `variant.json`

| Field | Meaning |
|---|---|
| `variant` | `ISO`; `ISO-SPEC` with `--task-md`; `ISO-AGENTS` with `--agents-md`; `ISO-SPEC-AGENTS` with both |
| `step`, `steps`, `task_name` | N, `["step-N"]`, the task name |
| `start_state` | `reference_step` = N−1 and the hash of `environment/app/` |
| `task_md_sha256`, `task_md_replaced` | `task.md` of the step in the build |
| `agents_md_sha256`, `agents_md_replaced` | `AGENTS.md` of the initial commit; equals `hashes.agents_md` of `run.json` |
| `instruction_sha256` | equals `hashes.step_instructions["step-N"]` of `run.json` |
| `source_commit`, `source_bench_sha256`, `builder_sha256` | what the build was made from |

The runner copies `variant.json` into `run.json` as `task_variant`; `bench/report`
reads `task_variant.variant` as the name of the cell.

## What the verifier does in this mode

The verifier is the one of G0 with `BENCH_STEP = N`: common criteria, criteria of
step N, and the criteria of steps 1…N−1 (the cascade, rule C5). The reward is the
share of all of them, as at step N of G0. Two things follow from the fact that the
container never ran steps 1…N−1:

| What | In G0 at step N | Here |
|---|---|---|
| `regressions` of `criteria.json` | criteria that passed at an earlier step and fail now | always empty: there is no earlier record. A regression is a failed criterion whose `step` is less than N — the start state passes all of them (oracle gate) |
| `CJ1` (evidence on the agent's test suite) | diff of `tests/` against step N−1 | `skipped`: "no tests/ snapshot of step N−1" |

Other differences from step N of a cascade: the git history is one `Initial commit`
(no snapshot commits of earlier steps), `.venv` has the project installed at image
build, and the databases and the broker are fresh (no verifier run before the agent).

## Running

```bash
python3 bench-iso/build_variant.py --step 3
python3 bench/run/run_batch.py --model gemma-4-31b --harnesses opencode --repeats 3 --steps 1 --matrix \
  --task-dir bench-iso/build/iso-s3/bench
```

`--steps 1` is "the first 1 step of the task directory"; the built task has exactly one
step, `step-N`, so this runs step N. The step keeps its G0 name: `run.json`, the traffic
log and the snapshot use `step-N`.

Oracle and the empty agent (no model, no proxy):

```bash
(cd bench-iso/build/iso-s3 && uvx --offline --from harbor==0.23.0 harbor run -p bench -a oracle -o <out>)
(cd bench-iso/build/iso-s3 && uvx --offline --from harbor==0.23.0 harbor run -p bench -a nop -o <out>)
```
