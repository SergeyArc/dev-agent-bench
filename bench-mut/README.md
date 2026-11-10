# bench-mut — mutation score of the agent's own tests

Question: are the tests an agent writes real? Measure: the share of mutants of the agent's
`src/tracksvc/` that its **own** suite (`uv run pytest` in its `/app`) kills. The hidden
tests of the bench are not used anywhere here.

Tool: [mutmut](https://github.com/boxed/mutmut) 3.8.0, installed into the unit's `.venv`
inside the bench image (`dev-agent-bench:dev`); the unit's `pyproject.toml` and
`uv.lock` are not changed.

## Files

| File | Runs on | Purpose |
|---|---|---|
| `prepare_unit.sh` | host | plain copy of a project root: the oracle of step 5, or a snapshot bundle |
| `run_unit.sh` | host | one container per shard (entry of `src/tracksvc/`), shards in parallel |
| `in_container.sh` | image | precondition (clean suite, as C4) → mutmut → clean suite again |
| `plugin/benchmut_reset.py` | image | before every pytest run: kill orphans, recreate `tracksvc_pytest` |
| `batch.py` | host | many units through one queue of containers; load limits, rerun of timeouts, stop and resume (see its docstring) |
| `run_one.sh` | host | one container for one unit outside the batch (a clean run, or mutmut on a few globs) |
| `collect.py` | host | table by layer from the shards' results |
| `stage2.py` | host | tables of the full run from the batch results (units, layers, cells, timeouts) |
| `recheck_in_container.sh` | image | whole suite for every "no tests" mutant (checks that verdict) |
| `inventory.py`, `trials.tsv` | host | C4 outcome and code size of every step-5 unit, for the estimate |

## Usage

```bash
OUT=~/bench-runs/analysis/mutation
bench-mut/prepare_unit.sh reference "$OUT/units/reference"
bench-mut/prepare_unit.sh bundle <trial>/steps/step-5/artifacts/logs/artifacts/snapshot-step-5.bundle "$OUT/units/<name>"
bench-mut/run_unit.sh "$OUT/units/<name>" "$OUT/runs/<name>"
python3 bench-mut/collect.py "$OUT/runs/<name>" --json "$OUT/runs/<name>/summary.json"
```

## How a unit is run

- The unit's tree replaces `/app` of the image (its `.venv` and `.git` stay), `uv sync --frozen`.
- Precondition: `uv run pytest` passes on the unmutated tree, as the `agent` user, with the
  environment of criterion C4 — `DATABASE_URL` pointing to the empty database
  `tracksvc_pytest`, `RABBITMQ_URL` to the broker. A unit that fails is not mutated (exit 3).
- mutmut mutates `src/` and runs, for each mutant, the tests that call the mutated function,
  one mutant at a time (`--max-children 1`: the suite owns one database and one broker).
- Parallelism comes from shards: each has its own container, PostgreSQL and RabbitMQ.
- After the session the clean suite runs once more: the environment must still be healthy.

## Deviations from a plain `uv run pytest`, all forced by the tool

| What | Why |
|---|---|
| `process_isolation=forkserver`, `forkserver_warmup=none` | with the default (fork) a mutant inherits a process that already ran a pytest session; tests that pass in a fresh process then fail (alembic keeps the closed captured stdout) and their mutants count as killed |
| `PYTHONPATH=/app/mutants/src:…` | subprocesses the suite starts (worker, alembic) must import the mutated copy, not the editable install |
| `UV_PROJECT_ENVIRONMENT=/app/.venv`, `UV_NO_SYNC=1` | `uv run` started by the suite from `mutants/` must reuse the unit's environment |
| `-p benchmut_reset` | C4 gives the suite an empty database and stops agent processes afterwards; a session runs the suite hundreds of times in one container |
| `-x`, only the covering tests | mutmut's way of running a mutant |

## Known limits

- mutmut mutates function bodies only: module-level code, class bodies and decorators are not mutated.
- "no tests" means "no test calls the function in the test process". Code reached only in a
  subprocess is reported so; `recheck_in_container.sh` measures those mutants with the whole suite.
- Equivalent mutants are not triaged: the score is a lower bound.
- Per-mutant durations are not recorded by mutmut in forkserver mode; time per mutant is the
  shard's time divided by its mutants.
