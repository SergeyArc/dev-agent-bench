# Results

All figures below are for the main matrix variant **G0** unless noted. A cell is three trials; the table shows the median and the min–max range. **Where two pairs' ranges overlap, the order is not a ranking** — neither pair is called better.

Plots of every individual run: [`figures/fig-1-leaderboard.svg`](figures/fig-1-leaderboard.svg), cost vs score [`figures/fig-2-cost.svg`](figures/fig-2-cost.svg), model × harness [`figures/fig-3-model-harness.svg`](figures/fig-3-model-harness.svg).

## Leaderboard (eight pairs)

| Model | Harness | Rubric score, median [min–max] | Judge score, median (separate) |
|---|---|---|---|
| Gemma 4 31B | opencode | 0.80 [0.76–0.96] | 0.77 (n=2) |
| Qwen3.8-27B | opencode | 0.79 [0.78–0.96] | 0.87 (n=3) |
| Qwen3.8-27B | OpenHands | 0.59 [0.59–0.79] | 0.83 (n=3) |
| Nemotron 3 Super | OpenHands | 0.55 [0.54–0.72] | 0.58 (n=3) |
| Devstral 2 | OpenHands | 0.48 [0.31–0.69] | 0.58 (n=3) |
| Devstral 2 | opencode | 0.46 [0.46–0.50] | 0.64 (n=3) |
| Nemotron 3 Super | opencode | 0.42 [0.29–0.62] | 0.63 (n=3) |
| Gemma 4 31B | OpenHands | 0.41 [0.19–0.57] | 0.59 (n=3) |

Sorted by rubric median. n is the number of runs the judge scored. Nemotron × OpenHands is shown with the 600-turn limit. For Gemma × opencode the judge scored two runs out of three: in the third the agent damaged its own git history and no code snapshots were left.

The two leading pairs are indistinguishable at this sample size. No run scored 1.0; the best single G0 runs reached 0.96.

## Reliability of step criteria

Over 68 step criteria × 8 pairs × 3 trials, share in percent: mean trial / pass@3 (passed in at least one of three) / pass^3 (passed in all three):

| Pair | mean / pass@3 / pass^3 |
|---|---|
| Gemma × opencode | 92 / 99 / 81 |
| Qwen × opencode | 89 / 100 / 69 |
| Qwen × OpenHands | 71 / 71 / 71 |
| Devstral × OpenHands | 67 / 87 / 32 |
| Nemotron × opencode | 62 / 85 / 35 |
| Nemotron × OpenHands | 59 / 85 / 34 |
| Devstral × opencode | 50 / 65 / 37 |
| Gemma × OpenHands | 37 / 63 / 18 |

Shared static checks and the judge are not in this table. “Hard” step criteria (passed in at most 40% of the 24 G0 runs) are almost all on step 5, so “hard criterion” is mixed with “step 5 did not finish”.

## Cost

Same rubric band, different price among the leaders: a Gemma × opencode run takes about 72 minutes and 145k completion tokens; a Qwen × opencode run takes about 227 minutes and 596k.

## Harness vs model

On Gemma, opencode 0.80 vs OpenHands 0.41 — ranges do not overlap. On Qwen the ranges barely overlap. On Nemotron and Devstral the harness gap sits inside the run-to-run spread. One harness setting changes the picture: Nemotron × OpenHands with a 300-turn limit scores 0.20 [0.17–0.55]; with 600 turns, 0.55 [0.54–0.72].

The hardest single check — `/ready` must return 503 within 10 seconds after the database goes silent mid-flight — passed in 2 of 24 G0 runs.

## Ablation G2 (pre-commit `make check`)

On Gemma × opencode, G0 rubric trials 0.96 / 0.80 / 0.76 vs G2 0.92 / 0.93 / 0.91 (n=3). On Qwen × opencode, G0 0.96 / 0.79 / 0.78 vs G2 0.19 / 0.80 / 0.99. Ranges overlap; with n=3 the ablation is not claimed as a ranking win. The reliable G2 effect on Qwen was the linter check (C1): 10/15 steps in G0 vs 15/15 in G2.

## Failure modes (qualitative)

Recurring patterns from trajectory review (no raw paths published here):

- **Readiness after a silent database** — typical failure hangs past the deadline while holding a pooled connection; the rare passes use a fresh short-lived connection or a driver-level command timeout.
- **Broken file writes on OpenHands** — unquoted heredoc writes that duplicate content and leave a literal `EOF` line in source, then a loop detector cuts the session.
- **Loop detector** — identical tool actions repeated a few times end the step mid-edit.
- **Reasoning without action** — long reasoning with no tool call hits the output token cap; the step ends without code changes.
- **History compaction vs project rules** — after the harness compresses history, agents stop making the required final commit even when a pre-commit hook is present.

## Mutation score of the agent's tests

mutmut 3.8.0 on `src/tracksvc/` at step 5; only the agent's own suite kills mutants. Score exists only when that suite is green. Reference score 0.78. G0 cell medians (min–max), where measured: Qwen × OpenHands 0.70 (0.52–0.76); Gemma × opencode 0.58 (0.58–0.58); Qwen × opencode 0.53 (0.44–0.65); Devstral × OpenHands 0.27; Devstral × opencode 0.23 (0.19–0.36); Nemotron × opencode 0.06 (0.05–0.07). A large share of mutants never touch any test; the worker layer is often untested in G0.
