# Self-test fixtures for the step-4 criteria

Used only by `benchverify.selftest_step4` (never by a trial run, never as a
reference solution; the conventions of `AGENTS.md` are not followed on purpose).
`base/` is a minimal service that keeps tracks in PostgreSQL and satisfies the
deterministic criteria of steps 1–4 and the common ones; the empty `__init__.py`
files of the package are written by the self-test (`selftest_step4.PACKAGES`).

Broken variants are listed in `selftest_step4.VARIANTS` with the criteria each
must break. A variant is an overlay directory here (`memory_store/`,
`no_code_unique/`) and/or text replacements applied to files of `base/`
(`no_row_lock`, `no_row_lock_rewrite`, `seal_before_check`, `cached_reads`).
