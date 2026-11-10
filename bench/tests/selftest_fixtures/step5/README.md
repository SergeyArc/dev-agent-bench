# Self-test fixtures for the step-5 criteria

Used only by `benchverify.selftest_step5` (never by a trial run, never as a
reference solution, and it does not follow the conventions of `AGENTS.md`).
`base/` is a minimal service for the whole contract: the HTTP API of steps 1–4
(tracks in PostgreSQL) and the worker of step 5. It satisfies criteria
5.1–5.19, 1.1–1.13 and the common deterministic criteria; the empty
`__init__.py` files of the package are written by the self-test itself
(`selftest_step5.PACKAGES`).

The broken variants are not directories: each one is a textual patch of
`base/src/tracksvc/worker.py`, listed with the criteria it must break in
`selftest_step5.VARIANTS`.
