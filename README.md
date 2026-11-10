# dev-agent-bench

**Do local open-weight LLM agents follow project conventions?**

## What this is

A [Harbor](https://pypi.org/project/harbor/) benchmark built around one realistic backend task. An agent writes a greenfield Python service — FastAPI, SQLAlchemy 2 (async), Alembic, aio-pika, PostgreSQL, RabbitMQ — with no human to ask.

- **Five cascading steps:** skeleton, API, domain rules, database, queue worker. Each step runs in a new agent session and continues the agent's own code from the previous step, so mistakes accumulate.
- **Project rules live in `AGENTS.md`:** layered architecture, invariants in the domain, the use case owns the transaction, strict typing and linting, no speculative abstractions, no weakening of tests. Working code is not enough; it has to be written the way the project requires.
- **The domain is synthetic** on purpose, so a model cannot recognise it from training data.

The rules the agent sees are in [`bench/environment/app/AGENTS.md`](bench/environment/app/AGENTS.md); the external contract of the service is in [`docs/contract.md`](docs/contract.md). Scoring layers, calibration and variants are described in [`docs/methodology.md`](docs/methodology.md).

## The matrix

4 models × 2 harnesses × 3 runs = 24 runs, five steps each.

- **Models (open weights):** Qwen3.8-27B, Gemma 4 31B, Nemotron 3 Super, Devstral 2.
- **Harnesses:** [opencode](https://opencode.ai/) 1.18.34 and [OpenHands](https://github.com/All-Hands-AI/OpenHands) 1.6.0.
- **Serving:** one server, one [vLLM](https://github.com/vllm-project/vllm), identical settings for all models.
- **Limit:** 2 hours per step.

A step scores from 0 to 1: the share of behaviour and rule checks passed. A failed required check or a timeout sets the step to zero. A run's score is the mean over its five steps. The table shows the median of three runs with the worst and best run in brackets. Full numbers, reliability, cost and failure modes: [`docs/results.md`](docs/results.md). Every individual run is plotted in [`docs/figures/fig-1-leaderboard.svg`](docs/figures/fig-1-leaderboard.svg).

| Model | Harness | Rubric score, median [min–max] | Judge score, median (separate, see note) |
|---|---|---|---|
| Gemma 4 31B | opencode | 0.80 [0.76–0.96] | 0.77 (n=2) |
| Qwen3.8-27B | opencode | 0.79 [0.78–0.96] | 0.87 (n=3) |
| Qwen3.8-27B | OpenHands | 0.59 [0.59–0.79] | 0.83 (n=3) |
| Nemotron 3 Super | OpenHands | 0.55 [0.54–0.72] | 0.58 (n=3) |
| Devstral 2 | OpenHands | 0.48 [0.31–0.69] | 0.58 (n=3) |
| Devstral 2 | opencode | 0.46 [0.46–0.50] | 0.64 (n=3) |
| Nemotron 3 Super | opencode | 0.42 [0.29–0.62] | 0.63 (n=3) |
| Gemma 4 31B | OpenHands | 0.41 [0.19–0.57] | 0.59 (n=3) |

Sorted by the rubric median; n is the number of runs the judge scored. Nemotron 3 Super on OpenHands is shown with the limit of 600 turns per step. The judge score (Claude Fable; calibrated against GPT-5.6-Sol) is a separate number and is not mixed into the rubric score.

- The two leading pairs are indistinguishable at this sample size. Where the ranges of two pairs overlap, the order in the table is not a ranking.
- No run scored 1.0. The best single runs reached 0.96.
- Same score, different price: Gemma × opencode about 72 minutes and 145k completion tokens per run; Qwen × opencode about 227 minutes and 596k.

## How it was scored

| Layer | What it checks | How |
|---|---|---|
| Behaviour | The service does what the task requires | 68 hidden black-box tests over HTTP and RabbitMQ |
| Project rules | The code is written the way the project requires | 8 shared static checks (linter, types, layers, agent tests, configs) |
| Architecture | What a test cannot catch | LLM judge, 22 criteria scored 0–2 — a separate score |

- **Gate.** The reference of every step scores 1.0. 23 broken versions of the reference are each caught by the tests of their own step.
- **Isolation.** Reference and tests were written without access to each other.
- **Judge calibration.** Exact agreement with a second-family judge on 27 of 30 sample units; disputed cases resolved by a human. Details in [`docs/methodology.md`](docs/methodology.md).

## Repository layout

| Path | What is inside |
|---|---|
| [`bench/`](bench/) | Harbor task: image, five steps, verifier, runner, rubric and judge |
| [`docs/`](docs/) | Contract, methodology, results, figures |
| [`serving/`](serving/) | vLLM launch scripts and pinned model settings |
| [`bench-g2/`](bench-g2/) | Variant with a pre-commit hook that runs the project checks |
| [`bench-iso/`](bench-iso/) | Isolated variant: one step from the reference of the previous step |
| [`bench-mut/`](bench-mut/) | Mutation score of the agent's own tests |

## Reproducing

What you need:

- Docker, Python 3.10 or newer, and [uv](https://astral.sh/uv/) to run Harbor 0.23.0 through `uvx`.
- An OpenAI-compatible endpoint that serves one of the models listed in [`serving/models.lock.json`](serving/models.lock.json), and its API key. The scripts in `serving/` show how the models were started with vLLM; paths and device names in them are placeholders.

```bash
# 1. The reference solution should score 1.0 on every step (no model needed)
uvx --from harbor==0.23.0 harbor run -p bench -a oracle -o <out>

# 2. One trial: one model, one harness, step 1 only
python3 bench/run/run_trial.py --model qwen3.8-27b --harness opencode --steps 1

# 3. A cell of the matrix: three runs of all five steps
python3 bench/run/run_batch.py --model gemma-4-31b --harnesses opencode --repeats 3 --steps 5 --matrix
```

More detail: [`bench/run/README.md`](bench/run/README.md), [`bench-g2/README.md`](bench-g2/README.md), [`bench-iso/README.md`](bench-iso/README.md), [`bench-mut/README.md`](bench-mut/README.md), [`serving/README.md`](serving/README.md).

What is **not** in this repository: per-trial run directories, trajectories, the operator runbook, or model weights. Numbers above can be re-measured with the code here, but not re-derived from raw traffic logs.

## Limitations

- 3 runs per pair. When the ranges of two pairs overlap, neither is called better.
- One task and one stack. On another task, stack or harness version the picture may differ.
- Failure causes were reconstructed from code and logs, not confirmed by separate experiments.

## License

MIT — see [`LICENSE`](LICENSE).
