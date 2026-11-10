"""Step criteria modules: `step<N>.py` with `register(suite, ctx)`.

Shared (task-level) on purpose: at step N the verifier re-runs the criteria of
steps 1…N-1 under their own IDs (rule C5), and Harbor uploads only the
task-level `tests/` plus the current step's `tests/` to `/tests`.
Empty in the skeleton — step criteria are written in separate tasks.
"""
