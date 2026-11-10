# Self-test fixtures for the step-3 criteria

Used only by `benchverify.selftest_step3` (never by a trial run, never as a reference
solution; the conventions of `AGENTS.md` are not followed). `base/` is laid over the
step-1 fixture (`../step1/base`: Alembic, `/ready` probe, `tests/test_health.py`) and
replaces its `api/app.py`: a minimal service with tracks in process memory that
satisfies the contract of steps 1–3 (R1–R5, the error body of §5) and the common
deterministic criteria.

The broken variants are not directories: each is one text replacement in a file of
`base/`, listed in `selftest_step3.VARIANTS` together with the criteria it must break.
