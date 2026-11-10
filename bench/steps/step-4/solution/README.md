# step-4 (L3-01) — oracle solution

- `app/` — the complete reference source tree for `/app` after step 4 (the step-3 tree
  with tracks stored in PostgreSQL): `src/tracksvc/`, `alembic.ini`, `migrations/`,
  `tests/`. `pyproject.toml`, `uv.lock`, `ruff.toml` and `mypy.ini` of the initial commit
  stay untouched; no dependency is added.
- `solve.sh` — removes from `src/`, `tests/` and `migrations/` the files that are not part
  of `app/` (the in-memory storage and its tests among them), then copies `app/` into
  `/app` (override the target with `APP_DIR`). Idempotent; runs as the `agent` user or as
  root.
