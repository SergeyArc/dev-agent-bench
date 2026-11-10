# Self-test fixtures for the step-2 criteria

Used only by `benchverify.selftest_step2` (never by a trial run, never as a reference
solution; the project conventions of `AGENTS.md` do not apply to it). `base/` is laid over
`../step1/base/`: together they are a minimal service that satisfies criteria 1.1–1.13,
2.1–2.9 and the common deterministic criteria. Every other directory is an overlay of files
that replaces part of the result to break exactly the criteria named in
`selftest_step2.VARIANTS`.
