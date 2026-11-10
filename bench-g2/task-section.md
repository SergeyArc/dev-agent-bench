
## Finishing the step

Before you finish, commit all your work with `git commit`. The repository has a pre-commit
hook that runs `make check` (`ruff`, the complexity limit, the import rules of the layers,
`mypy --strict`, `pytest`) and rejects the commit while any of them fails: fix the code and
commit again until the commit goes through. Do not bypass or change the hook — no
`--no-verify`, no changes to `Makefile`, `.importlinter` or `.githooks/`.
