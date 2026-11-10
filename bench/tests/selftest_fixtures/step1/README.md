# Self-test fixtures for the step-1 criteria

Used only by `benchverify.selftest` (never by a trial run, never as a reference
solution). `base/` is a minimal service that satisfies criteria 1.1–1.13 and the
common deterministic criteria; the empty `__init__.py` files of the package are
written by the self-test itself (`selftest.STEP1_PACKAGES`). Every other
directory is an overlay of files that replaces part of `base/` to break exactly
the criteria named in `selftest.STEP1_VARIANTS`.
