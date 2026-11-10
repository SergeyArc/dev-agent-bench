# Methodology

## Task and cascade

The benchmark is one greenfield Python service (FastAPI, SQLAlchemy 2 async, Alembic, aio-pika, PostgreSQL, RabbitMQ). The agent works in five cascading steps: skeleton, API, domain rules, database, queue worker. Each step starts a new agent session on the agent's own code from the previous step, so mistakes accumulate.

Project rules for the evaluated agent live in `bench/environment/app/AGENTS.md` (layered architecture, invariants in the domain, the use case owns the transaction, strict typing and linting, no speculative abstractions, no weakening of tests). The external service contract is `docs/contract.md`. The domain is synthetic on purpose.

## Isolation when building the task

The reference solution of each step was written without access to the hidden tests; the hidden tests were written without access to the reference. A mismatch between reference and tests is treated as ambiguity in the contract, not as a free pass for the agent.

## Gate: reference and broken versions

Every step's reference scores 1.0 on the verifier with no flaky tests. Twenty-three broken versions of the reference — plausible mistakes — are each caught by the tests of their own step (6 on step 1, then 4 / 4 / 4 / 5 on steps 2–5). A broken version counts as caught only when a step criterion fails, not when only the agent's own tests fail.

## Rubric (deterministic score)

A step scores from 0 to 1: the share of behaviour and rule checks passed. A failed required check or a timeout sets the step to zero. A run's score is the mean over its five steps. The published table shows the median of three runs with the worst and best run in brackets.

Deterministic criteria total 76:

- 68 black-box step checks over HTTP and RabbitMQ (13 / 9 / 16 / 11 / 19 by step). The agent never sees them.
- 8 shared weighted static checks: linter, types, layer boundaries, the agent's own tests pass, configs untouched — the same tools the project itself uses (C1, C2, C3, C4, C7, C8, N1, N2). Regression and unweighted checks are tracked separately and are not in the 76.

## Judge (separate score)

An LLM judge scores architecture that a test cannot catch (who owns the transaction, speculative abstractions). There are 22 criteria on a 0–2 scale (20 step-local plus shared CJ1 hygiene of tests and CJ2 public API docstrings). The judge score is **not** mixed into the rubric score and is **not** zeroed on a timeout or a failed required check.

The judge used for the matrix is Claude Fable (`claude -p`, no tools, one turn, empty working directory). The reference solutions were also written with Claude, so the judge was checked against a second judge from another family, GPT-5.6-Sol.

### Calibration

Without asking a human to label the whole set:

1. **Known answers** — units with an expected level from the reference and controlled variants (31/31 matched for Fable).
2. **Second judge** — GPT-5.6-Sol on a 30-unit sample: exact agreement with Fable on 27 of 30 (weighted κ 0.874).
3. Disagreements between the two judges were reviewed by a human.

After that check, the judge score is published as a separate column next to the rubric, not as a single combined ranking.

## Matrix, limits, serving

4 open-weight models × 2 harnesses × 3 runs = 24 runs on the main matrix (variant G0), five steps each.

- **Models:** Qwen3.8-27B, Gemma 4 31B, Nemotron 3 Super, Devstral 2.
- **Harnesses:** opencode 1.18.34 and OpenHands 1.6.0.
- **Serving:** one vLLM version and identical settings for all models (see `serving/README.md` and `serving/models.lock.json`). Profile P1: context 131072, fp8 KV.
- **Per-step limits:** 2 hours of agent time, 1M completion tokens, output cap 32 000 tokens. OpenHands turn limit: 300 for Gemma, 600 for Qwen, Nemotron and Devstral in the published cells (Nemotron × OpenHands was also run at 300 turns as a harness-setting row, not as the cell).

## Variants

| Variant | What changes |
|---|---|
| **G0** (`bench/`) | Main matrix task |
| **G2** (`bench-g2/`) | Same task plus a git pre-commit hook that runs `make check`, and an instruction to commit at the end of every step |
| **G2b** | G2 plus the commit rule also stated in `AGENTS.md` |
| **iso** (`bench-iso/`) | One step N, started from the reference tree of step N−1; optional replacement of `task.md` / `AGENTS.md` for wording edits |
| **mut** (`bench-mut/`) | Mutation score of the agent's own tests on the step-5 snapshot (hidden bench tests are not used) |
