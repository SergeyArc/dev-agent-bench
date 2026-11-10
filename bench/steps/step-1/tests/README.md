# step-1 (L1-01) — tests

No step-specific verifier files: step 1 is graded by the shared entry point
`tests/test.sh` (task level) with `BENCH_STEP=1`, which runs
reset → common criteria C1–C8 → `tests/criteria/step1.py` (criteria 1.1–1.13)
and writes `criteria.json` and `reward.json`.

The criteria module lives at task level because step N also re-runs the
criteria of steps 1…N-1 (rule C5), and Harbor uploads only the task-level
`tests/` plus the current step's `tests/` to `/tests`. A `test.sh` placed here
would only shadow the shared one.

Judge criteria (CJ2, 1.J1, 1.J3) are not evaluated yet; the rubric / judge
files of this step go here.
