# step-4 (L3-01) — tests

Placeholder. Step-specific verifier files (rubric, fixtures) go here.

The verifier entry point and the shared library live in the task-level
`tests/` directory; Harbor uploads it to `/tests` first and layers this
directory on top. Step criteria modules live in `tests/criteria/step<N>.py`
(task level), because step N also re-runs the criteria of steps 1…N-1.
